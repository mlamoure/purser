"""Bulk lot captures: a broker's lot list as of a day, previewed against the
book and committed under the plan hash the preview returned (DESIGN section
8, issue #393, decision 107).

- ``preview`` writes nothing. It returns the plan per symbol (changes and
  conflicts, from ``core.lots.plan_capture``) and ``plan_hash``: a keyed hash
  of the request, the plan and a digest of the state the plan was computed
  from. Any write to those positions in between changes it.
- ``commit`` takes the account lock (the lock order every holdings writer
  uses), computes the preview again and refuses (``PreviewStale``) when its
  hash is not the one the caller passed; refuses (``CaptureConflicts``) a
  plan with conflicts unless ``skip_conflicts`` (then a symbol with any
  conflict is left out whole); archives the verbatim request in
  ``raw_fetches`` (raw-first); records the capture; then applies.
- The exact request again (same keyed ``request_hash``: same as-of,
  captured-at, source note and rows) is a retry: nothing is written, and the
  first commit's capture is returned, also to a writer that waited on the
  lock behind it. The same economic content observed again (a new
  captured-at or as-of) is a repeat observation: a new capture row records
  the freshness (``repeat_of``), and the plan, being empty, changes nothing.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import audit, clock, crypto, events
from purser import ledger as ledger_service
from purser.core import holdings as core
from purser.core import lots as core_lots
from purser.holdings import lotbook, projection
from purser.holdings import service as holdings
from purser.ledger import LedgerError
from purser.models import (
    Account,
    ApiToken,
    HoldingsCapture,
    Lot,
    Position,
    RawFetch,
)

COMPLETE = "complete"
PARTIAL = "partial"
COMPLETENESS = (COMPLETE, PARTIAL)
KEEP = "keep"
REPLACE = "replace"
BASIS_POLICIES = (KEEP, REPLACE)
MAX_ROWS = 2000
MAX_SYMBOLS = 50
CLOCK_SKEW = timedelta(minutes=10)
RAW_SOURCE = "capture"


class PreviewStale(LedgerError):
    """The state changed since the preview: its hash no longer matches
    (REST 409 ``preview_stale``, carrying the fresh preview)."""

    def __init__(self, preview: "Preview") -> None:
        super().__init__(
            "the holdings changed since this capture was previewed; review the new plan and "
            "commit with its plan_hash"
        )
        self.preview = preview


class CaptureConflicts(LedgerError):
    """The plan has conflicts and ``skip_conflicts`` was not given (REST 409
    ``capture_conflicts``)."""

    def __init__(self, preview: "Preview") -> None:
        n = sum(len(s.plan.conflicts) for s in preview.symbols)
        super().__init__(
            f"the capture has {n} conflict(s); resolve them (record the sales or transfers "
            "out, void wrong lots) and capture again, or commit with skip_conflicts to apply "
            "the symbols that have none"
        )
        self.preview = preview


@dataclass(frozen=True)
class LotIn:
    acquired_on: date
    quantity: Decimal
    external_ref: str | None = None
    plan: str | None = None
    ordinal: int | None = None
    basis_per_share: Decimal | None = None
    cost_basis: Decimal | None = None
    acquisition: str = "unknown"


@dataclass(frozen=True)
class SymbolIn:
    symbol: str
    lots: tuple[LotIn, ...]
    name: str | None = None


@dataclass(frozen=True)
class LotCaptureIn:
    account: str
    as_of: date
    captured_at: datetime
    completeness: str
    positions: tuple[SymbolIn, ...]
    source_note: str | None = None
    basis_policy: str = KEEP


@dataclass(frozen=True)
class SymbolPreview:
    symbol: str
    position_id: uuid.UUID | None
    stale: bool
    latest_capture_as_of: date | None
    plan: core_lots.SymbolPlan
    rows: tuple[core_lots.CaptureRow, ...]
    inputs: tuple[LotIn, ...]
    name: str | None


@dataclass(frozen=True)
class Preview:
    account: Account
    capture: LotCaptureIn
    request_hash: str
    content_hash: str
    plan_hash: str
    canonical: str
    symbols: tuple[SymbolPreview, ...]
    already_applied: HoldingsCapture | None = None
    repeat_of: uuid.UUID | None = None

    @property
    def conflicts(self) -> int:
        return sum(len(s.plan.conflicts) for s in self.symbols)


@dataclass(frozen=True)
class Applied:
    preview: Preview
    capture: HoldingsCapture
    retry: bool  # the exact request was applied before: nothing written now
    created_lots: tuple[uuid.UUID, ...] = ()
    skipped_symbols: tuple[str, ...] = ()
    result: dict[str, Any] = field(default_factory=dict)


def _q(value: Decimal | None, quant: Decimal) -> str | None:
    return None if value is None else str(value.quantize(quant))


def _canonical_lot(lot: LotIn) -> dict[str, Any]:
    return {
        "acquired_on": lot.acquired_on.isoformat(),
        "quantity": _q(lot.quantity, core.QUANTITY_QUANT),
        "external_ref": lotbook.normalize_ref(lot.external_ref) if lot.external_ref else None,
        "plan": lot.plan.strip() if lot.plan else None,
        "ordinal": lot.ordinal,
        "basis_per_share": _q(lot.basis_per_share, core.PRICE_QUANT),
        "cost_basis": _q(lot.cost_basis, core.MONEY_QUANT),
        "acquisition": lot.acquisition,
    }


def _canonical(capture: LotCaptureIn, account: Account) -> tuple[str, str]:
    """(the whole request, its economic content) as canonical JSON. The
    content leaves out the as-of, the captured-at, the source note and row
    order: the same holdings observed again hash the same."""
    positions: list[dict[str, Any]] = []
    economic: list[dict[str, Any]] = []
    for p in capture.positions:
        symbol = holdings.symbol_of(p.symbol)
        rows = [_canonical_lot(lot) for lot in p.lots]
        positions.append({"symbol": symbol, "name": holdings.name_of(p.name), "lots": rows})
        economic.append(
            {"symbol": symbol, "lots": sorted(json.dumps(r, sort_keys=True) for r in rows)}
        )
    whole = {
        "kind": "lots",
        "account_id": str(account.id),
        "as_of": capture.as_of.isoformat(),
        "captured_at": capture.captured_at.astimezone(UTC).isoformat(),
        "completeness": capture.completeness,
        "basis_policy": capture.basis_policy,
        "source_note": capture.source_note,
        "positions": positions,
    }
    content = {
        "kind": "lots",
        "account_id": str(account.id),
        "completeness": capture.completeness,
        "positions": sorted(economic, key=lambda p: str(p["symbol"])),
    }
    return canonical_json(whole), canonical_json(content)


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def keyed(text: str) -> str:
    return crypto.keyed_hash(hashlib.sha256(text.encode()).hexdigest())


def _validate(capture: LotCaptureIn, today: date, now: datetime) -> None:
    if capture.completeness not in COMPLETENESS:
        raise LedgerError(f"completeness must be one of: {', '.join(COMPLETENESS)}")
    if capture.basis_policy not in BASIS_POLICIES:
        raise LedgerError(f"basis_policy must be one of: {', '.join(BASIS_POLICIES)}")
    if capture.as_of > today:
        raise LedgerError(
            f"as_of {capture.as_of.isoformat()} is in the future; a capture is of holdings "
            "as they stood"
        )
    if capture.captured_at.tzinfo is None:
        raise LedgerError("captured_at needs a time zone (an ISO instant)")
    if capture.captured_at > now + CLOCK_SKEW:
        raise LedgerError("captured_at is in the future")
    if not capture.positions:
        raise LedgerError("a capture lists at least one symbol")
    if len(capture.positions) > MAX_SYMBOLS:
        raise LedgerError(f"a capture lists at most {MAX_SYMBOLS} symbols")
    if sum(len(p.lots) for p in capture.positions) > MAX_ROWS:
        raise LedgerError(f"a capture lists at most {MAX_ROWS} lots")
    seen: set[str] = set()
    for p in capture.positions:
        symbol = holdings.symbol_of(p.symbol)
        if symbol in seen:
            raise LedgerError(f"{symbol} is listed twice; list each symbol once")
        seen.add(symbol)
    if capture.source_note is not None and len(capture.source_note) > 500:
        raise LedgerError("source_note is at most 500 characters")


def _rows(symbol: str, lots_in: Sequence[LotIn], as_of: date) -> list[core_lots.CaptureRow]:
    rows: list[core_lots.CaptureRow] = []
    for i, lot in enumerate(lots_in):
        try:
            quantity = core.check_quantity(lot.quantity, f"{symbol} row {i}: quantity")
            basis = (
                core_lots.normalize_basis(quantity, lot.basis_per_share, lot.cost_basis)
                if quantity > 0
                else core_lots.LotBasisInput(None, None, None)
            )
            if lot.ordinal is not None and lot.ordinal < 1:
                raise ValueError(f"{symbol} row {i}: ordinal starts at 1")
            rows.append(
                core_lots.CaptureRow(
                    index=i,
                    ref=lotbook.ref_index(lot.external_ref) if lot.external_ref else None,
                    acquired_on=lot.acquired_on,
                    plan=lotbook.plan_index(lot.plan) if lot.plan else None,
                    ordinal=lot.ordinal,
                    quantity=quantity,
                    basis=basis,
                    acquisition=lot.acquisition,
                )
            )
        except (ValueError, core_lots.LotError) as exc:
            raise LedgerError(f"{symbol} row {i}: {exc}") from exc
    try:
        return core_lots.validate_rows(rows, as_of)
    except core_lots.LotError as exc:
        raise LedgerError(f"{symbol}: {exc}") from exc


def latest_capture(
    session: Session, account_id: uuid.UUID, kind: str, symbol: str | None = None
) -> tuple[date, datetime] | None:
    """The newest applied capture of a kind (for lots, one covering
    ``symbol``), ordered by its as-of day, then by when it was read: two
    captures of one day are ordered by ``captured_at``."""
    stmt = select(HoldingsCapture.as_of, HoldingsCapture.captured_at).where(
        HoldingsCapture.account_id == account_id, HoldingsCapture.kind == kind
    )
    if symbol is not None:
        stmt = stmt.where(HoldingsCapture.symbols.contains([symbol]))
    row = session.execute(
        stmt.order_by(HoldingsCapture.as_of.desc(), HoldingsCapture.captured_at.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row is not None else None


def is_stale(as_of: date, captured_at: datetime, latest: tuple[date, datetime] | None) -> bool:
    """A capture older than the newest applied one: an earlier as-of day, or
    the same day read earlier. Compared, never applied."""
    return latest is not None and (as_of, captured_at) < latest


def _state(
    position: Position | None,
    facts: Sequence[core.Event],
    loaded: lotbook.Loaded | None,
    book: core_lots.Book,
    latest: date | None,
) -> dict[str, Any]:
    """Everything the plan of one symbol read, as a fingerprint."""
    if position is None:
        return {"position": None, "latest": latest.isoformat() if latest else None}
    return {
        "position": str(position.id),
        "events": sorted(
            (str(e.id), e.occurred_on.isoformat(), e.kind, str(e.quantity))
            for e in core.effective_events(facts)
        ),
        "lots": sorted(
            (
                str(r.id),
                str(r.quantity),
                r.effective_on.isoformat(),
                str(r.basis_per_share),
                str(r.cost_basis),
                r.external_ref_bidx or "",
                r.plan_bidx or "",
                r.ordinal,
            )
            for r in (loaded.lots if loaded else [])
        ),
        "disposals": sorted(str(d.event_id) + str(d.lot_id) for d in book.disposals),
        "latest": latest.isoformat() if latest else None,
    }


def _plan_json(plan: core_lots.SymbolPlan) -> dict[str, Any]:
    return {
        "changes": [
            [c.kind, c.row, str(c.lot_id) if c.lot_id else None, str(c.quantity), c.ordinal]
            for c in plan.changes
        ],
        "conflicts": [[c.code, c.row, str(c.lot_id) if c.lot_id else None] for c in plan.conflicts],
        "target": str(plan.target_quantity),
    }


def preview(
    session: Session,
    capture: LotCaptureIn,
    *,
    today: date | None = None,
    now: datetime | None = None,
    account: Account | None = None,
) -> Preview:
    """The plan of a lot capture; writes nothing (decision 107)."""
    today = today or clock.ledger_today()
    now = now or datetime.now(UTC)
    if account is None:
        account = ledger_service.resolve_account(session, capture.account)
    holdings.require_manual(account)
    _validate(capture, today, now)
    whole, content = _canonical(capture, account)
    request_hash = keyed(whole)
    content_hash = keyed(content)
    existing = session.execute(
        select(HoldingsCapture).where(
            HoldingsCapture.account_id == account.id,
            HoldingsCapture.kind == "lots",
            HoldingsCapture.request_hash == request_hash,
        )
    ).scalar_one_or_none()
    previous = session.execute(
        select(HoldingsCapture.id, HoldingsCapture.content_hash)
        .where(HoldingsCapture.account_id == account.id, HoldingsCapture.kind == "lots")
        .order_by(HoldingsCapture.created_at.desc())
        .limit(1)
    ).first()
    repeat_of = (
        previous.id if previous is not None and previous.content_hash == content_hash else None
    )
    symbols: list[SymbolPreview] = []
    states: list[dict[str, Any]] = []
    for p in sorted(capture.positions, key=lambda p: holdings.symbol_of(p.symbol)):
        symbol = holdings.symbol_of(p.symbol)
        rows = _rows(symbol, p.lots, capture.as_of)
        position = holdings.open_position(session, account.id, symbol)
        facts: list[core.Event] = []
        loaded: lotbook.Loaded | None = None
        if position is not None:
            facts = [
                projection.event_fact(r)
                for r in projection.position_events(session, [position.id]).get(position.id, [])
            ]
            loaded = lotbook.load(session, [position.id]).get(position.id)
        book = lotbook.book(loaded, facts)
        disposed = {d.lot_id for d in book.disposals if d.lot_id is not None}
        live_ids = {lot.id for lot in book.lots}
        known = [
            core_lots.KnownLot(
                lotbook.lot_fact(r), r.external_ref_bidx, r.plan_bidx, r.id in disposed
            )
            for r in (loaded.lots if loaded else [])
            if r.id in live_ids
        ]
        newest = latest_capture(session, account.id, "lots", symbol)
        stale = is_stale(capture.as_of, capture.captured_at, newest)
        latest = newest[0] if newest is not None else None
        try:
            plan = core_lots.plan_capture(
                rows,
                known,
                facts,
                book,
                as_of=capture.as_of,
                complete=capture.completeness == COMPLETE,
                stale=stale,
                replace_basis=capture.basis_policy == REPLACE,
            )
        except core.OutOfRange as exc:
            raise LedgerError(f"{symbol}: {exc}") from exc
        symbols.append(
            SymbolPreview(
                symbol,
                position.id if position else None,
                stale,
                latest,
                plan,
                tuple(rows),
                p.lots,
                p.name,
            )
        )
        states.append(
            {
                "symbol": symbol,
                "newest": str(newest),
                "state": _state(position, facts, loaded, book, latest),
                "plan": _plan_json(plan),
            }
        )
    plan_hash = keyed(
        json.dumps({"request": request_hash, "symbols": states}, sort_keys=True, default=str)
    )
    return Preview(
        account=account,
        capture=capture,
        request_hash=request_hash,
        content_hash=content_hash,
        plan_hash=plan_hash,
        canonical=whole,
        symbols=tuple(symbols),
        already_applied=existing,
        repeat_of=repeat_of,
    )


def commit(
    session: Session,
    capture: LotCaptureIn,
    *,
    expect_hash: str,
    skip_conflicts: bool = False,
    token: ApiToken | None,
    today: date | None = None,
    now: datetime | None = None,
) -> Applied:
    """Apply a previewed capture under the account lock (decision 107)."""
    today = today or clock.ledger_today()
    resolved = ledger_service.resolve_account(session, capture.account)
    account = projection.lock_account(session, resolved.id)
    planned = preview(session, capture, today=today, now=now, account=account)
    if planned.already_applied is not None:
        return Applied(
            preview=planned,
            capture=planned.already_applied,
            retry=True,
            result=planned.already_applied.result,
        )
    if planned.plan_hash != expect_hash:
        raise PreviewStale(planned)
    if planned.conflicts and not skip_conflicts:
        raise CaptureConflicts(planned)
    applying = [s for s in planned.symbols if not s.plan.conflicts]
    skipped = tuple(s.symbol for s in planned.symbols if s.plan.conflicts)

    # Raw-first: the verbatim request before anything it plans is written.
    raw = RawFetch(
        connection_id=None,
        source=RAW_SOURCE,
        content_hash=hashlib.sha256(planned.canonical.encode()).hexdigest(),
        payload_enc=crypto.encrypt(planned.canonical),
    )
    session.add(raw)
    session.flush()

    new_ids: dict[tuple[str, int], uuid.UUID] = {}
    for s in applying:
        for c in s.plan.changes:
            if c.kind == "new_lot" and c.row is not None:
                new_ids[(s.symbol, c.row)] = uuid.uuid4()
    result = _result(planned, applying, skipped, new_ids)
    record = HoldingsCapture(
        account_id=account.id,
        kind="lots",
        as_of=capture.as_of,
        captured_at=capture.captured_at,
        completeness=capture.completeness,
        symbols=[s.symbol for s in applying],
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

    project_from: date | None = None
    for s in applying:
        first = _apply_symbol(session, account, s, record, new_ids, token=token, today=today)
        if first is not None:
            project_from = first if project_from is None else min(project_from, first)
    if project_from is not None:
        projection.project_account(session, account, from_date=project_from, today=today)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="capture.apply",
        entity_type="holdings_capture",
        entity_id=record.id,
        after={"account_id": str(account.id), **result},
    )
    events.emit(
        session,
        type_="holdings.captured",
        entity_type="holdings_capture",
        entity_id=record.id,
        payload={
            "account": account.path,
            "kind": "lots",
            "as_of": capture.as_of.isoformat(),
            "completeness": capture.completeness,
            "symbols": [s.symbol for s in applying],
            "skipped_symbols": list(skipped),
            "changes": result["counts"],
            "repeat": planned.repeat_of is not None,
        },
    )
    return Applied(
        preview=planned,
        capture=record,
        retry=False,
        created_lots=tuple(new_ids.values()),
        skipped_symbols=skipped,
        result=result,
    )


def _result(
    planned: Preview,
    applying: Sequence[SymbolPreview],
    skipped: Sequence[str],
    new_ids: dict[tuple[str, int], uuid.UUID],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for s in applying:
        for c in s.plan.changes:
            counts[c.kind] = counts.get(c.kind, 0) + 1
    return {
        "as_of": planned.capture.as_of.isoformat(),
        "completeness": planned.capture.completeness,
        "counts": counts,
        "unchanged": sum(s.plan.unchanged for s in applying),
        "skipped_zero": sum(s.plan.skipped_zero for s in applying),
        "not_in_capture": sum(s.plan.not_in_capture for s in applying),
        "symbols": [s.symbol for s in applying],
        "skipped_symbols": list(skipped),
        "skipped_conflicts": sorted(
            {c.code for s in planned.symbols if s.symbol in skipped for c in s.plan.conflicts}
        ),
        "created_lots": [str(v) for v in new_ids.values()],
        "repeat_of": str(planned.repeat_of) if planned.repeat_of else None,
    }


def _apply_symbol(
    session: Session,
    account: Account,
    s: SymbolPreview,
    record: HoldingsCapture,
    new_ids: dict[tuple[str, int], uuid.UUID],
    *,
    token: ApiToken | None,
    today: date,
) -> date | None:
    """Apply one symbol's plan; returns the first date its rows change from."""
    if not s.plan.changes:
        return None
    capture = record
    as_of = capture.as_of
    position = holdings.get_position(session, s.position_id) if s.position_id is not None else None
    changes = list(s.plan.changes)
    recount = next((c for c in changes if c.kind == "recount"), None)
    first = as_of
    if position is None:
        position = holdings._new_position(
            session, account, symbol=s.symbol, name=s.name, opened_on=as_of, token=token
        )
    source = f"capture {record.id}"
    if recount is not None and recount.quantity is not None:
        holdings._record(
            session,
            account,
            position,
            kind=core.SET_QUANTITY,
            occurred_on=as_of,
            quantity=recount.quantity,
            note="recount from a complete lot capture",
            source_note=source,
            token=token,
            today=today,
            defer_projection=True,
        )
    by_row = {r.index: r for r in s.rows}
    for c in changes:
        if c.kind == "new_lot":
            assert c.row is not None
            row = by_row[c.row]
            given = s.inputs[c.row]
            session.add(
                Lot(
                    id=new_ids[(s.symbol, c.row)],
                    position_id=position.id,
                    acquired_on=row.acquired_on,
                    effective_on=as_of,
                    quantity=row.quantity,
                    basis_per_share=row.basis.basis_per_share,
                    cost_basis=row.basis.cost_basis,
                    basis_source=row.basis.source,
                    acquisition=row.acquisition,
                    plan=given.plan.strip() if given.plan else None,
                    plan_bidx=row.plan,
                    external_ref=lotbook.normalize_ref(given.external_ref)
                    if given.external_ref
                    else None,
                    external_ref_bidx=row.ref,
                    ordinal=c.ordinal or 1,
                    capture_id=record.id,
                    origin=holdings._lot_origin(token),
                    actor_token_id=token.id if token else None,
                )
            )
        elif c.kind in ("fill_basis", "replace_basis"):
            assert c.row is not None and c.lot_id is not None
            row = by_row[c.row]
            lot = session.get(Lot, c.lot_id)
            assert lot is not None
            # The row's basis as the broker gave it — a total stays the total
            # (authoritative), never rebuilt from a rounded per-share figure.
            # A lot whose basis may change has no dispositions, so its
            # remaining shares on as_of are its quantity: the row's basis is
            # the lot's.
            if Decimal(lot.quantity) == row.quantity:
                basis = row.basis
            else:  # unreachable while the planner gates on dispositions
                basis = core_lots.normalize_basis(
                    Decimal(lot.quantity), row.basis.basis_per_share, None
                )
            audit.record(
                session,
                actor_token_id=token.id if token else None,
                action="lot.basis",
                entity_type="lot",
                entity_id=lot.id,
                before={
                    "basis_per_share": str(lot.basis_per_share) if lot.basis_per_share else None,
                    "cost_basis": str(lot.cost_basis) if lot.cost_basis is not None else None,
                },
                after={
                    "basis_per_share": str(basis.basis_per_share),
                    "cost_basis": str(basis.cost_basis),
                    "capture_id": str(record.id),
                    "change": c.kind,
                },
            )
            lot.basis_per_share = basis.basis_per_share
            lot.cost_basis = basis.cost_basis
            lot.basis_source = basis.source
            first = min(first, lot.effective_on)
        elif c.kind == "assign_ref":
            assert c.row is not None and c.lot_id is not None
            given = s.inputs[c.row]
            lot = session.get(Lot, c.lot_id)
            assert lot is not None and given.external_ref
            lot.external_ref = lotbook.normalize_ref(given.external_ref)
            lot.external_ref_bidx = lotbook.ref_index(given.external_ref)
            audit.record(
                session,
                actor_token_id=token.id if token else None,
                action="lot.ref",
                entity_type="lot",
                entity_id=lot.id,
                after={"capture_id": str(record.id), "external_ref": "set"},
            )
    session.flush()
    holdings._check_timeline(session, position)
    return first
