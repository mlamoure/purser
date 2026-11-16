"""Equity awards: grants, vest schedules, award captures and vests (DESIGN
section 8, issue #393, decision 107).

Awards are informational: owned holdings, balances and net-worth history
do not read ``awards`` or ``award_vests``. Home may add their value in a
separate potential-value scenario. A vest becomes owned shares only
through ``record_vest``, which either records a ``vest`` event (net shares
on the position) together with the lot holding them, or LINKS a lot that is
already there (adopted from the broker's lot list), adding no shares. Either
way the vest row turns ``vested`` and points at its lot, so its units leave
the unvested total exactly when the lot starts counting: never twice, never
missing.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import audit, balances, clock, crypto, events
from purser import ledger as ledger_service
from purser.core import awards as core_awards
from purser.core import holdings as core
from purser.core import lots as core_lots
from purser.holdings import captures, lotbook, projection
from purser.holdings import service as holdings
from purser.ledger import LedgerError, NotFoundError
from purser.models import (
    Account,
    ApiToken,
    Award,
    AwardVest,
    HoldingsCapture,
    Lot,
    Position,
    PositionEvent,
    RawFetch,
)

MAX_AWARDS = 200


class VestConflict(LedgerError):
    """A vest already recorded differently, or forfeited (REST 409)."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VestIn:
    vest_date: date
    quantity: Decimal
    ordinal: int | None = None


@dataclass(frozen=True)
class AwardIn:
    grant_ref: str
    symbol: str
    granted_on: date
    granted_quantity: Decimal
    cancelled_quantity: Decimal | None = None
    vested_quantity: Decimal | None = None
    plan: str | None = None
    schedule: tuple[VestIn, ...] = ()


@dataclass(frozen=True)
class AwardCaptureIn:
    account: str
    as_of: date
    captured_at: datetime
    completeness: str
    awards: tuple[AwardIn, ...]
    source_note: str | None = None


def _account(session: Session, ref: str) -> Account:
    account = ledger_service.resolve_account(session, ref)
    if account.is_virtual or account.type != "brokerage":
        raise LedgerError(f"{account.path} is not a brokerage account; awards belong to one")
    if account.closed_at is not None:
        raise LedgerError(f"account {account.path} is closed (read-only)")
    return account


def _ordinals(schedule: Sequence[VestIn]) -> tuple[core_awards.VestIn, ...]:
    """A vest's ordinal defaults to 1 when it alone has its date."""
    dates: dict[date, int] = {}
    for v in schedule:
        dates[v.vest_date] = dates.get(v.vest_date, 0) + 1
    out = []
    for v in schedule:
        if v.ordinal is None and dates[v.vest_date] > 1:
            raise LedgerError(
                f"two vests on {v.vest_date.isoformat()}: give each a distinct ordinal"
            )
        out.append(
            core_awards.VestIn(
                v.vest_date, holdings.checked(core.check_quantity, v.quantity), v.ordinal or 1
            )
        )
    return tuple(out)


def _canonical(capture: AwardCaptureIn, account: Account) -> tuple[str, str]:
    def q(value: Decimal | None) -> str | None:
        return None if value is None else str(value.quantize(core.QUANTITY_QUANT))

    awards = [
        {
            "grant_ref": lotbook.normalize_ref(a.grant_ref),
            "symbol": holdings.symbol_of(a.symbol),
            "granted_on": a.granted_on.isoformat(),
            "granted": q(a.granted_quantity),
            "cancelled": q(a.cancelled_quantity),
            "vested": q(a.vested_quantity),
            "plan": a.plan.strip() if a.plan else None,
            "schedule": [[v.vest_date.isoformat(), q(v.quantity), v.ordinal] for v in a.schedule],
        }
        for a in capture.awards
    ]
    whole = {
        "kind": "awards",
        "account_id": str(account.id),
        "as_of": capture.as_of.isoformat(),
        "captured_at": capture.captured_at.astimezone(UTC).isoformat(),
        "completeness": capture.completeness,
        "source_note": capture.source_note,
        "awards": awards,
    }
    content = {
        "kind": "awards",
        "account_id": str(account.id),
        "completeness": capture.completeness,
        "awards": sorted(
            (
                {**a, "schedule": sorted(a["schedule"], key=captures.canonical_json)}  # type: ignore[arg-type]
                for a in awards
            ),
            key=lambda a: str(a["grant_ref"]),
        ),
    }
    return captures.canonical_json(whole), captures.canonical_json(content)


# --------------------------------------------------------------------------
# Captures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AwardPreview:
    account: Account
    capture: AwardCaptureIn
    request_hash: str
    content_hash: str
    plan_hash: str
    canonical: str
    plan: core_awards.AwardsPlan
    stale: bool
    latest_capture_as_of: date | None
    already_applied: HoldingsCapture | None = None
    repeat_of: uuid.UUID | None = None

    @property
    def conflicts(self) -> int:
        return len(self.plan.conflicts)


@dataclass(frozen=True)
class AwardApplied:
    preview: AwardPreview
    capture: HoldingsCapture
    retry: bool
    result: dict[str, Any] = field(default_factory=dict)


class AwardPreviewStale(LedgerError):
    def __init__(self, preview: AwardPreview) -> None:
        super().__init__(
            "the awards changed since this capture was previewed; review the new plan and "
            "commit with its plan_hash"
        )
        self.preview = preview


class AwardCaptureConflicts(LedgerError):
    def __init__(self, preview: AwardPreview) -> None:
        super().__init__(
            f"the capture has {preview.conflicts} conflict(s); resolve them and capture again, "
            "or commit with skip_conflicts to apply the awards that have none"
        )
        self.preview = preview


def _known(session: Session, account: Account) -> list[core_awards.KnownAward]:
    rows = list(session.execute(select(Award).where(Award.account_id == account.id)).scalars())
    vests: dict[uuid.UUID, list[AwardVest]] = {}
    if rows:
        for v in session.execute(
            select(AwardVest).where(AwardVest.award_id.in_([r.id for r in rows]))
        ).scalars():
            vests.setdefault(v.award_id, []).append(v)
    return [
        core_awards.KnownAward(
            id=r.id,
            grant_ref=r.grant_ref_bidx,
            symbol=r.symbol,
            granted_on=r.granted_on,
            granted=Decimal(r.granted_quantity),
            cancelled=Decimal(r.cancelled_quantity),
            reported_vested=(
                Decimal(r.reported_vested_quantity)
                if r.reported_vested_quantity is not None
                else None
            ),
            vests=tuple(
                core_awards.KnownVest(
                    v.id,
                    v.vest_date,
                    v.ordinal,
                    Decimal(v.quantity),
                    v.status,
                    Decimal(v.vested_gross) if v.vested_gross is not None else None,
                )
                for v in sorted(vests.get(r.id, []), key=lambda v: (v.vest_date, v.ordinal))
            ),
            reported_as_of=r.reported_as_of,
        )
        for r in rows
    ]


def _plan_state(plan: core_awards.AwardsPlan) -> dict[str, Any]:
    return {
        "changes": [
            [
                c.kind,
                c.award,
                str(c.award_id),
                str(c.vest_id),
                str(c.vest_date),
                c.ordinal,
                str(c.quantity),
            ]
            for c in plan.changes
        ],
        "conflicts": [[c.code, c.award, str(c.award_id), str(c.vest_id)] for c in plan.conflicts],
    }


def preview_awards(
    session: Session,
    capture: AwardCaptureIn,
    *,
    today: date | None = None,
    now: datetime | None = None,
    account: Account | None = None,
) -> AwardPreview:
    today = today or clock.ledger_today()
    now = now or datetime.now(UTC)
    account = account or _account(session, capture.account)
    if capture.completeness not in captures.COMPLETENESS:
        raise LedgerError(f"completeness must be one of: {', '.join(captures.COMPLETENESS)}")
    if capture.as_of > today:
        raise LedgerError(f"as_of {capture.as_of.isoformat()} is in the future")
    if capture.captured_at.tzinfo is None:
        raise LedgerError("captured_at needs a time zone (an ISO instant)")
    if capture.captured_at > now + captures.CLOCK_SKEW:
        raise LedgerError("captured_at is in the future")
    if not capture.awards:
        raise LedgerError("an award capture lists at least one award")
    if len(capture.awards) > MAX_AWARDS:
        raise LedgerError(f"an award capture lists at most {MAX_AWARDS} awards")
    if capture.source_note is not None and len(capture.source_note) > 500:
        raise LedgerError("source_note is at most 500 characters")
    whole, content = _canonical(capture, account)
    request_hash = captures.keyed(whole)
    content_hash = captures.keyed(content)
    inputs = [
        core_awards.AwardIn(
            index=i,
            grant_ref=holdings.checked(lotbook.ref_index, a.grant_ref, lotbook.GRANT_NAMESPACE),
            symbol=holdings.symbol_of(a.symbol),
            granted_on=a.granted_on,
            granted=holdings.checked(core.check_quantity, a.granted_quantity, "granted_quantity"),
            cancelled=(
                holdings.checked(core.check_quantity, a.cancelled_quantity, "cancelled_quantity")
                if a.cancelled_quantity is not None
                else None
            ),
            reported_vested=(
                holdings.checked(core.check_quantity, a.vested_quantity, "vested_quantity")
                if a.vested_quantity is not None
                else None
            ),
            schedule=_ordinals(a.schedule),
        )
        for i, a in enumerate(capture.awards)
    ]
    try:
        core_awards.validate(inputs)
    except core_awards.AwardError as exc:
        raise LedgerError(str(exc)) from exc
    known = _known(session, account)
    newest = captures.latest_capture(session, account.id, "awards")
    stale = captures.is_stale(capture.as_of, capture.captured_at, newest)
    latest = newest[0] if newest is not None else None
    plan = core_awards.plan_awards(
        inputs,
        known,
        as_of=capture.as_of,
        complete=capture.completeness == captures.COMPLETE,
        stale=stale,
    )
    existing = session.execute(
        select(HoldingsCapture).where(
            HoldingsCapture.account_id == account.id,
            HoldingsCapture.kind == "awards",
            HoldingsCapture.request_hash == request_hash,
        )
    ).scalar_one_or_none()
    previous = session.execute(
        select(HoldingsCapture.id, HoldingsCapture.content_hash)
        .where(HoldingsCapture.account_id == account.id, HoldingsCapture.kind == "awards")
        .order_by(HoldingsCapture.created_at.desc())
        .limit(1)
    ).first()
    state = {
        "awards": sorted(
            (
                str(k.id),
                k.grant_ref,
                str(k.cancelled),
                str(k.reported_vested),
                sorted((str(v.id), v.status, str(v.quantity)) for v in k.vests),
            )
            for k in known
        ),
        "latest": str(newest),
    }
    plan_hash = captures.keyed(
        captures.canonical_json(
            {"request": request_hash, "state": state, "plan": _plan_state(plan)}
        )
    )
    return AwardPreview(
        account=account,
        capture=capture,
        request_hash=request_hash,
        content_hash=content_hash,
        plan_hash=plan_hash,
        canonical=whole,
        plan=plan,
        stale=stale,
        latest_capture_as_of=latest,
        already_applied=existing,
        repeat_of=(
            previous.id if previous is not None and previous.content_hash == content_hash else None
        ),
    )


def commit_awards(
    session: Session,
    capture: AwardCaptureIn,
    *,
    expect_hash: str,
    skip_conflicts: bool = False,
    token: ApiToken | None,
    today: date | None = None,
    now: datetime | None = None,
) -> AwardApplied:
    today = today or clock.ledger_today()
    resolved = _account(session, capture.account)
    account = projection.lock_account(session, resolved.id)
    planned = preview_awards(session, capture, today=today, now=now, account=account)
    if planned.already_applied is not None:
        return AwardApplied(planned, planned.already_applied, True, planned.already_applied.result)
    if planned.plan_hash != expect_hash:
        raise AwardPreviewStale(planned)
    if planned.conflicts and not skip_conflicts:
        raise AwardCaptureConflicts(planned)
    raw = RawFetch(
        connection_id=None,
        source=captures.RAW_SOURCE,
        content_hash=hashlib.sha256(planned.canonical.encode()).hexdigest(),
        payload_enc=crypto.encrypt(planned.canonical),
    )
    session.add(raw)
    session.flush()
    counts: dict[str, int] = {}
    for c in planned.plan.changes:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    result = {
        "as_of": capture.as_of.isoformat(),
        "completeness": capture.completeness,
        "counts": counts,
        "unchanged": planned.plan.unchanged,
        "due_unrecorded": planned.plan.due_unrecorded,
        "not_in_capture": planned.plan.not_in_capture,
        "skipped_conflicts": sorted({c.code for c in planned.plan.conflicts}),
        "repeat_of": str(planned.repeat_of) if planned.repeat_of else None,
    }
    record = HoldingsCapture(
        account_id=account.id,
        kind="awards",
        as_of=capture.as_of,
        captured_at=capture.captured_at,
        completeness=capture.completeness,
        symbols=sorted({holdings.symbol_of(a.symbol) for a in capture.awards}),
        request_hash=planned.request_hash,
        content_hash=planned.content_hash,
        plan_hash=planned.plan_hash,
        repeat_of=planned.repeat_of,
        raw_fetch_id=raw.id,
        source_note=capture.source_note,
        result=result,
        origin=holdings._lot_origin(token),
        actor_token_id=token.id if token else None,
    )
    session.add(record)
    session.flush()
    created: dict[int, Award] = {}
    for c in planned.plan.changes:
        given = capture.awards[c.award] if c.award is not None else None
        if c.kind == "new_award":
            assert given is not None and c.award is not None
            award = Award(
                account_id=account.id,
                symbol=holdings.symbol_of(given.symbol),
                grant_ref=lotbook.normalize_ref(given.grant_ref),
                grant_ref_bidx=lotbook.ref_index(given.grant_ref, lotbook.GRANT_NAMESPACE),
                granted_on=given.granted_on,
                plan=given.plan.strip() if given.plan else None,
                granted_quantity=given.granted_quantity,
                cancelled_quantity=given.cancelled_quantity or Decimal(0),
                reported_vested_quantity=given.vested_quantity,
                reported_as_of=capture.as_of if given.vested_quantity is not None else None,
                origin=holdings._lot_origin(token),
                actor_token_id=token.id if token else None,
            )
            session.add(award)
            session.flush()
            created[c.award] = award
        elif c.kind == "new_vest":
            award_id = c.award_id or (created[c.award].id if c.award is not None else None)
            assert award_id is not None and c.vest_date is not None and c.quantity is not None
            session.add(
                AwardVest(
                    award_id=award_id,
                    vest_date=c.vest_date,
                    ordinal=c.ordinal or 1,
                    quantity=c.quantity,
                    status="scheduled",
                )
            )
        elif c.kind == "reschedule":
            vest = session.get(AwardVest, c.vest_id)
            assert vest is not None and c.quantity is not None
            vest.quantity = c.quantity
            vest.updated_at = func.now()
        elif c.kind == "cancel":
            target = session.get(Award, c.award_id)
            assert target is not None and c.quantity is not None
            target.cancelled_quantity = c.quantity
            target.updated_at = func.now()
        elif c.kind == "reported_vested":
            target = session.get(Award, c.award_id)
            assert target is not None
            target.reported_vested_quantity = c.quantity
            target.reported_as_of = capture.as_of
            target.updated_at = func.now()
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="capture.apply",
        entity_type="holdings_capture",
        entity_id=record.id,
        after={
            "account_id": str(account.id),
            "kind": "awards",
            **result,
            "changes": [
                {
                    "kind": c.kind,
                    "award_id": str(
                        c.award_id or (created[c.award].id if c.award in created else "")
                    ),
                    "vest_date": c.vest_date.isoformat() if c.vest_date else None,
                    "quantity": str(c.quantity) if c.quantity is not None else None,
                    "before": str(c.before) if c.before is not None else None,
                }
                for c in planned.plan.changes
            ],
        },
    )
    events.emit(
        session,
        type_="holdings.captured",
        entity_type="holdings_capture",
        entity_id=record.id,
        payload={
            "account": account.path,
            "kind": "awards",
            "as_of": capture.as_of.isoformat(),
            "completeness": capture.completeness,
            "changes": counts,
            "repeat": planned.repeat_of is not None,
        },
    )
    return AwardApplied(planned, record, False, result)


# --------------------------------------------------------------------------
# Vests
# --------------------------------------------------------------------------


def get_award(session: Session, award_id: uuid.UUID) -> Award:
    award = session.get(Award, award_id)
    if award is None:
        raise NotFoundError(f"award {award_id} not found")
    return award


def resolve_award(session: Session, *, award: str, account: str | None) -> Award:
    """An award by id, or by its grant reference on ``account`` (blind
    index: the reference never needs to be shown)."""
    try:
        return get_award(session, uuid.UUID(award))
    except ValueError:
        pass
    if account is None:
        raise LedgerError("name the account to find an award by its grant reference")
    acct = ledger_service.resolve_account(session, account)
    found = session.execute(
        select(Award).where(
            Award.account_id == acct.id,
            Award.grant_ref_bidx
            == holdings.checked(lotbook.ref_index, award, lotbook.GRANT_NAMESPACE),
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(f"{acct.path} has no award with that grant reference")
    return found


@dataclass(frozen=True)
class VestOutcome:
    award: Award
    vest: AwardVest | None  # None on a dry run of a new row
    lot_id: uuid.UUID | None
    event_id: uuid.UUID | None
    position_id: uuid.UUID | None
    linked: bool  # an existing lot was linked; no shares added
    already_recorded: bool
    dry_run: bool
    gross: Decimal
    withheld: Decimal
    net: Decimal
    fmv: Decimal | None


def _same_vest(
    vest: AwardVest, gross: Decimal, withheld: Decimal, net: Decimal, fmv: Decimal | None
) -> bool:
    return (
        Decimal(vest.vested_gross or 0) == gross
        and Decimal(vest.vested_withheld or 0) == withheld
        and Decimal(vest.vested_net or 0) == net
        and (Decimal(vest.vested_fmv) if vest.vested_fmv is not None else None) == fmv
    )


def record_vest(
    session: Session,
    *,
    award: str,
    account: str | None = None,
    vest_date: date,
    ordinal: int | None = None,
    gross_quantity: Decimal,
    withheld_quantity: Decimal = Decimal(0),
    net_quantity: Decimal | None = None,
    fmv: Decimal | None = None,
    lot: str | None = None,
    lot_ref: str | None = None,
    plan: str | None = None,
    force_new_lot: bool = False,
    note: str | None = None,
    source_note: str | None = None,
    token: ApiToken | None,
    dry_run: bool = False,
    today: date | None = None,
) -> VestOutcome:
    """Record a vest of ``award`` (decision 107).

    - The same vest recorded again with the same figures is a no-op
      (``already_recorded``); with other figures it is a ``VestConflict``.
    - ``lot`` (an id or the broker's lot reference) LINKS a lot the position
      already holds (adopted from a capture): same account and symbol,
      acquired on the vest date, the net shares (an adopted lot may hold
      fewer, sold before it was captured; an event-opened one exactly), and
      the fair market value as its basis when both are known. No event, no
      shares added.
    - Otherwise a ``vest`` event adds the net shares and opens their lot
      (basis: the fair market value per share; ``lot_ref`` its reference).
      Refused when the position already holds an unlinked lot acquired that
      day (``possible_duplicate``): link it, or pass ``force_new_lot``.
    """
    today = today or clock.ledger_today()
    found = resolve_award(session, award=award, account=account)
    acct = projection.lock_account(session, found.account_id)
    session.refresh(found)
    if vest_date > today:
        raise LedgerError("a vest is recorded once it has happened (vest_date is in the future)")
    if vest_date < found.granted_on:
        raise LedgerError("a vest cannot be dated before its grant")
    q = core.QUANTITY_QUANT
    gross = holdings.checked(core.check_quantity, gross_quantity, "gross_quantity").quantize(q)
    withheld = holdings.checked(
        core.check_quantity, withheld_quantity, "withheld_quantity"
    ).quantize(q)
    net = gross - withheld
    if net_quantity is not None and holdings.checked(core.check_quantity, net_quantity) != net:
        raise LedgerError(
            f"net_quantity {net_quantity} is not gross {gross} less withheld {withheld}"
        )
    if gross <= 0 or net < 0:
        raise LedgerError(
            "a vest has gross units above zero and withholds at most all of them (net >= 0)"
        )
    if net == 0 and (lot is not None or lot_ref is not None or force_new_lot):
        raise LedgerError(
            "every unit of this vest was withheld: it delivers no shares, so there is no lot "
            "to open or link"
        )
    if net > 0:
        holdings.require_manual(acct)
    if fmv is not None:
        fmv = holdings.checked(core.check_price, fmv, "fmv")
    if lot is not None and (lot_ref is not None or force_new_lot):
        raise LedgerError("lot links an existing lot; lot_ref and force_new_lot open a new one")
    vest = session.execute(
        select(AwardVest)
        .where(
            AwardVest.award_id == found.id,
            AwardVest.vest_date == vest_date,
            AwardVest.ordinal == (ordinal or 1),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if vest is not None and vest.status == "vested":
        if _same_vest(vest, gross, withheld, net, fmv):
            return VestOutcome(
                found,
                vest,
                vest.lot_id,
                vest.vest_event_id,
                None,
                vest.vest_event_id is None,
                True,
                dry_run,
                gross,
                withheld,
                net,
                fmv,
            )
        raise VestConflict(
            f"the {vest_date.isoformat()} vest of this award is already recorded with other "
            f"figures (gross {vest.vested_gross}, withheld {vest.vested_withheld}, fmv "
            f"{vest.vested_fmv}); reverse its vest event (or set it back to scheduled if it "
            "was linked) before recording it again"
        )
    if vest is not None and vest.status == "forfeited":
        raise VestConflict(
            f"the {vest_date.isoformat()} vest of this award is forfeited; set it back to "
            "scheduled first if it did vest"
        )
    # The vest as it will stand, checked against the grant (every path:
    # a scheduled row becoming vested, or a vest with no row yet).
    check_entitlement(
        session,
        found,
        replace={vest.id: ("vested", gross)} if vest is not None else {},
        add=[core_awards.VestFact(vest_date, "vested", gross, gross)] if vest is None else [],
    )

    def run() -> VestOutcome:
        nonlocal vest
        position = holdings.open_position(session, acct.id, found.symbol)
        event_id: uuid.UUID | None = None
        lot_id: uuid.UUID | None = None
        if net == 0:
            pass  # all withheld: a record of the vest, no shares, no lot
        elif lot is not None:
            if position is None:
                raise LedgerError(f"{acct.path} has no open {found.symbol} position to link")
            linked = _linkable(session, found, position, lot, vest_date, net, fmv)
            lot_id = linked.id
        else:
            if position is not None and not force_new_lot:
                _refuse_duplicate(session, position, vest_date)
            if position is None:
                position = holdings._new_position(
                    session,
                    acct,
                    symbol=found.symbol,
                    name=None,
                    opened_on=vest_date,
                    token=token,
                )
            written = holdings._record(
                session,
                acct,
                position,
                kind=core.VEST,
                occurred_on=vest_date,
                quantity=net,
                price=fmv,
                gross_quantity=gross,
                withheld_quantity=withheld,
                lot=holdings.LotSpec(acquisition="rsu_vest", external_ref=lot_ref, plan=plan),
                note=note,
                source_note=source_note,
                token=token,
                today=today,
            )
            assert written.lot is not None
            lot_id = written.lot.id
            event_id = written.event.id
        if vest is None:
            vest = AwardVest(
                award_id=found.id,
                vest_date=vest_date,
                ordinal=ordinal or 1,
                quantity=gross,
                status="vested",
            )
            session.add(vest)
        before = vest.status if vest.id else None
        vest.status = "vested"
        vest.lot_id = lot_id
        vest.vest_event_id = event_id
        vest.vested_gross = gross
        vest.vested_withheld = withheld
        vest.vested_net = net
        vest.vested_fmv = fmv
        vest.updated_at = func.now()
        session.flush()
        audit.record(
            session,
            actor_token_id=token.id if token else None,
            action="award_vest.vest",
            entity_type="award_vest",
            entity_id=vest.id,
            before={"status": before} if before else None,
            after={
                "award_id": str(found.id),
                "vest_date": vest_date.isoformat(),
                "gross": str(gross),
                "withheld": str(withheld),
                "net": str(net),
                "fmv": str(fmv) if fmv is not None else None,
                "lot_id": str(lot_id) if lot_id else None,
                "vest_event_id": str(event_id) if event_id else None,
                "linked": lot is not None,
                "note": note or None,
            },
        )
        events.emit(
            session,
            type_="award.updated",
            entity_type="award",
            entity_id=found.id,
            payload={
                "account": acct.path,
                "award_id": str(found.id),
                "symbol": found.symbol,
                "change": "vested",
                "vest_date": vest_date.isoformat(),
                "net_quantity": str(net),
                "linked": lot is not None,
            },
        )
        return VestOutcome(
            found,
            vest,
            lot_id,
            event_id,
            position.id if position is not None else None,
            lot is not None,
            False,
            dry_run,
            gross,
            withheld,
            net,
            fmv,
        )

    if not dry_run:
        return run()
    with balances.suspend_marks(session):
        savepoint = session.begin_nested()
        try:
            out = run()
            return VestOutcome(
                out.award,
                None,
                None,
                None,
                out.position_id,
                out.linked,
                False,
                True,
                gross,
                withheld,
                net,
                fmv,
            )
        finally:
            savepoint.rollback()


def check_entitlement(
    session: Session,
    award: Award,
    *,
    replace: dict[uuid.UUID, tuple[str, Decimal]] | None = None,
    add: list[core_awards.VestFact] | None = None,
    granted: Decimal | None = None,
    cancelled: Decimal | None = None,
) -> core_awards.Entitlement:
    """The award's entitlement after a change (a vest row's new status and
    units in ``replace``, new vests in ``add``, new granted or cancelled
    units), refused (422) when the evidence then adds up to more than was
    granted. The same calculation the planner and the summaries use
    (``core.awards.entitlement``), so no path can exceed a grant."""
    replace = replace or {}
    vests: list[core_awards.VestFact] = []
    for row in session.execute(select(AwardVest).where(AwardVest.award_id == award.id)).scalars():
        if row.id in replace:
            status, units = replace[row.id]
            vests.append(core_awards.VestFact(row.vest_date, status, Decimal(row.quantity), units))
        else:
            vests.append(_fact(row))
    vests.extend(add or [])
    out = core_awards.entitlement(
        granted=granted if granted is not None else Decimal(award.granted_quantity),
        cancelled=cancelled if cancelled is not None else Decimal(award.cancelled_quantity),
        reported_vested=(
            Decimal(award.reported_vested_quantity)
            if award.reported_vested_quantity is not None
            else None
        ),
        reported_as_of=award.reported_as_of,
        vests=vests,
    )
    if out.over:
        raise LedgerError(
            f"the award's vested ({out.vested}, counting the broker's reported baseline), "
            f"scheduled ({out.scheduled}) and cancelled ({out.cancelled}) units would exceed "
            f"the {out.granted} granted"
        )
    return out


def _fact(row: AwardVest) -> core_awards.VestFact:
    return core_awards.VestFact(
        row.vest_date,
        row.status,
        Decimal(row.quantity),
        Decimal(row.vested_gross) if row.vested_gross is not None else None,
    )


def _linkable(
    session: Session,
    award: Award,
    position: Position,
    ref: str,
    vest_date: date,
    net: Decimal,
    fmv: Decimal | None,
) -> Lot:
    from purser.holdings import lots as lot_service

    lot_id = lot_service.resolve_lot(session, position, ref)
    row = session.get(Lot, lot_id)
    assert row is not None
    if row.voided_at is not None:
        raise LedgerError(f"lot {row.id} is voided")
    if position.account_id != award.account_id or position.symbol != award.symbol:
        raise LedgerError("the lot is not on the award's account and symbol")
    facts = [
        projection.event_fact(r)
        for r in projection.position_events(session, [position.id]).get(position.id, [])
    ]
    book = lotbook.book(lotbook.load(session, [position.id]).get(position.id), facts)
    if book.lot(row.id) is None:
        raise LedgerError(f"lot {row.id} is not live (its opening event was reversed)")
    if row.acquired_on != vest_date:
        raise LedgerError(
            f"lot {row.id} was acquired {row.acquired_on.isoformat()}, not on the vest date "
            f"{vest_date.isoformat()}"
        )
    quantity = Decimal(row.quantity)
    if row.capture_id is None and quantity != net:
        raise LedgerError(f"lot {row.id} holds {quantity} shares; the vest delivered {net}")
    if quantity > net:
        raise LedgerError(
            f"lot {row.id} holds {quantity} shares, more than the {net} the vest delivered"
        )
    if fmv is not None and row.basis_per_share is not None:
        diff = abs(Decimal(row.basis_per_share) - fmv) * quantity
        if diff > core_lots.basis_tolerance(quantity):
            raise LedgerError(
                f"lot {row.id}'s basis per share ({row.basis_per_share}) is not the vest's "
                f"fair market value ({fmv})"
            )
    taken = session.execute(select(AwardVest.id).where(AwardVest.lot_id == row.id)).first()
    if taken is not None:
        raise VestConflict(f"lot {row.id} is already linked to award vest {taken[0]}")
    return row


def _refuse_duplicate(session: Session, position: Position, vest_date: date) -> None:
    rows = session.execute(
        select(Lot.id).where(
            Lot.position_id == position.id,
            Lot.acquired_on == vest_date,
            Lot.voided_at.is_(None),
            ~select(AwardVest.id).where(AwardVest.lot_id == Lot.id).exists(),
            # A lot whose opening event was reversed is dead (and voided by
            # the reversal): never a duplicate.
            ~select(PositionEvent.id)
            .where(PositionEvent.reverses_id == Lot.opened_by_event_id)
            .exists(),
        )
    ).scalars()
    found = list(rows)
    if found:
        raise VestConflict(
            f"possible_duplicate: {position.symbol} already holds {len(found)} unlinked lot(s) "
            f"acquired {vest_date.isoformat()} ({', '.join(str(i) for i in found)}); pass lot "
            "to link the one holding this vest's shares (no shares are added), or "
            "force_new_lot if this vest is a different lot"
        )


def update_vest(
    session: Session,
    *,
    vest_id: uuid.UUID,
    status: str,
    note: str | None,
    token: ApiToken | None,
) -> AwardVest:
    """Forfeit a scheduled vest, reinstate a forfeited one, or set a LINKED
    vest back to scheduled (a vest Purser recorded as an event goes back by
    reversing that event)."""
    vest = session.get(AwardVest, vest_id)
    if vest is None:
        raise NotFoundError(f"award vest {vest_id} not found")
    award = get_award(session, vest.award_id)
    projection.lock_account(session, award.account_id)
    session.refresh(vest)
    if status not in ("scheduled", "forfeited"):
        raise LedgerError("status must be scheduled or forfeited (record_vest vests)")
    before = {"status": vest.status, "lot_id": str(vest.lot_id) if vest.lot_id else None}
    if vest.status == status:
        return vest
    if status == "scheduled":
        # Checked before any change is flushed (the vested shape CHECK).
        check_entitlement(session, award, replace={vest.id: ("scheduled", Decimal(0))})
    if vest.status == "vested":
        if status != "scheduled":
            raise LedgerError("a vested vest goes back to scheduled first")
        if vest.vest_event_id is not None:
            raise LedgerError(
                f"this vest was recorded as event {vest.vest_event_id}; reverse that event "
                "(reverse_position_event) and the vest returns to scheduled"
            )
        vest.lot_id = None
        vest.vested_gross = vest.vested_withheld = vest.vested_net = vest.vested_fmv = None
    vest.status = status
    vest.updated_at = func.now()
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="award_vest.update",
        entity_type="award_vest",
        entity_id=vest.id,
        before=before,
        after={"status": status, "note": note or None},
    )
    events.emit(
        session,
        type_="award.updated",
        entity_type="award",
        entity_id=award.id,
        payload={
            "award_id": str(award.id),
            "symbol": award.symbol,
            "change": status,
            "vest_date": vest.vest_date.isoformat(),
        },
    )
    return vest


def update_award(
    session: Session,
    *,
    award_id: uuid.UUID,
    cancelled_quantity: Decimal | None = None,
    granted_quantity: Decimal | None = None,
    note: str | None = None,
    token: ApiToken | None,
) -> Award:
    """Correct an award's granted or cancelled units (audited)."""
    award = get_award(session, award_id)
    projection.lock_account(session, award.account_id)
    session.refresh(award)
    before = {
        "granted_quantity": str(award.granted_quantity),
        "cancelled_quantity": str(award.cancelled_quantity),
    }
    granted = Decimal(award.granted_quantity)
    cancelled = Decimal(award.cancelled_quantity)
    if granted_quantity is not None:
        granted = holdings.checked(core.check_quantity, granted_quantity, "granted_quantity")
        if granted <= 0:
            raise LedgerError("granted units must be above zero")
    if cancelled_quantity is not None:
        cancelled = holdings.checked(core.check_quantity, cancelled_quantity, "cancelled_quantity")
    if cancelled > granted:
        raise LedgerError("cancelled units cannot exceed granted units")
    check_entitlement(session, award, granted=granted, cancelled=cancelled)
    award.granted_quantity = granted
    award.cancelled_quantity = cancelled
    if note is not None:
        award.note = note or None
    award.updated_at = func.now()
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="award.update",
        entity_type="award",
        entity_id=award.id,
        before=before,
        after={
            "granted_quantity": str(granted),
            "cancelled_quantity": str(cancelled),
            "note": award.note,
        },
    )
    events.emit(
        session,
        type_="award.updated",
        entity_type="award",
        entity_id=award.id,
        payload={"award_id": str(award.id), "symbol": award.symbol, "change": "updated"},
    )
    return award


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AwardView:
    award: Award
    vests: list[AwardVest]
    vested: Decimal  # the baseline plus the vests recorded after it
    unvested: Decimal  # remaining when known, else the scheduled units
    forfeited: Decimal
    next_vest: AwardVest | None
    due_unrecorded: int
    scheduled: Decimal = Decimal(0)
    unscheduled: Decimal | None = None  # remaining units with no vest date; None: unknown
    unvested_known: bool = True  # False: no baseline, and the evidence leaves units unknown


@dataclass(frozen=True)
class Unvested:
    """Units not yet vested, valued at the latest close for information only:
    never in owned net worth, balances or holdings totals."""

    symbol: str
    units: Decimal
    value: Decimal | None
    price: core.PricePoint | None
    next_vest_date: date | None
    next_vest_units: Decimal | None
    awards: int
    unscheduled: Decimal = Decimal(0)  # of the units, those with no vest date yet
    complete: bool = True  # False: some award's remaining units are unknown


def award_views(
    session: Session,
    account: Account,
    *,
    symbol: str | None = None,
    today: date | None = None,
) -> list[AwardView]:
    today = today or clock.ledger_today()
    stmt = select(Award).where(Award.account_id == account.id)
    if symbol:
        stmt = stmt.where(Award.symbol == holdings.symbol_of(symbol))
    rows = list(session.execute(stmt.order_by(Award.granted_on, Award.id)).scalars())
    vests: dict[uuid.UUID, list[AwardVest]] = {}
    if rows:
        for v in session.execute(
            select(AwardVest)
            .where(AwardVest.award_id.in_([r.id for r in rows]))
            .order_by(AwardVest.vest_date, AwardVest.ordinal)
        ).scalars():
            vests.setdefault(v.award_id, []).append(v)
    out = []
    for r in rows:
        vs = vests.get(r.id, [])
        scheduled = [v for v in vs if v.status == "scheduled"]
        upcoming = [v for v in scheduled if v.vest_date > today]
        ent = core_awards.entitlement(
            granted=Decimal(r.granted_quantity),
            cancelled=Decimal(r.cancelled_quantity),
            reported_vested=(
                Decimal(r.reported_vested_quantity)
                if r.reported_vested_quantity is not None
                else None
            ),
            reported_as_of=r.reported_as_of,
            vests=[_fact(v) for v in vs],
        )
        out.append(
            AwardView(
                award=r,
                vests=vs,
                vested=ent.vested,
                unvested=ent.unvested,
                forfeited=sum(
                    (Decimal(v.quantity) for v in vs if v.status == "forfeited"), Decimal(0)
                ),
                next_vest=upcoming[0] if upcoming else None,
                due_unrecorded=len(scheduled) - len(upcoming),
                scheduled=ent.scheduled,
                unscheduled=ent.unscheduled,
                unvested_known=ent.remaining is not None,
            )
        )
    return out


def unvested(session: Session, account: Account, *, today: date | None = None) -> list[Unvested]:
    today = today or clock.ledger_today()
    views = award_views(session, account, today=today)
    by_symbol: dict[str, list[AwardView]] = {}
    for v in views:
        if v.unvested > 0:
            by_symbol.setdefault(v.award.symbol, []).append(v)
    books = projection.price_books(session, set(by_symbol), upper=today)
    out = []
    for symbol, vs in sorted(by_symbol.items()):
        units = sum((v.unvested for v in vs), Decimal(0))
        book = books.get(symbol)
        close = book.on(today) if book is not None else None
        point, value = core.valued(units, close)
        nexts = [v.next_vest for v in vs if v.next_vest is not None]
        first = min((n.vest_date for n in nexts), default=None)
        out.append(
            Unvested(
                symbol=symbol,
                units=units,
                value=value,
                price=point,
                next_vest_date=first,
                next_vest_units=(
                    sum((Decimal(n.quantity) for n in nexts if n.vest_date == first), Decimal(0))
                    if first
                    else None
                ),
                awards=len(vs),
                unscheduled=sum((v.unscheduled or Decimal(0) for v in vs), Decimal(0)),
                complete=all(v.unvested_known for v in vs),
            )
        )
    return out
