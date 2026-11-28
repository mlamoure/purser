"""Lot, capture and award tools (issue #393, DESIGN sections 8/10, decision
107). Registered on import.

Reads are read scope and paged to the response budget (guardrail 6): a
position's lots with its lot summary (``list_lots``), awards with the
unvested total and next vest (``list_awards``), and realized gains by term
(``realized_gains``) — aggregates, never row dumps an agent must add up.

Writes are write scope and go through the REST paths' services:
``upsert_lots`` / ``upsert_awards`` are the bulk captures (dry run by
default; commit with the preview's ``plan_hash`` as ``expect_hash``),
``record_vest``, ``record_transfer_out``, ``void_lot``,
``correct_lot_basis`` and ``update_award_vest`` the single changes.

The broker's lot and grant references are account identifiers: always
withheld unless the call includes ``external_ids``; they are still accepted
as INPUT wherever a lot or award is named. Plan labels are narrative
(withheld when the call passes withhold=["description"]); per-lot basis,
proceeds and gains are single amounts (withheld with withhold=["amount"]).
"""

import json
import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from mcp.types import CallToolResult
from sqlalchemy.orm import Session

from purser import clock
from purser.mcp import presentation
from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.mcp.tools_holdings import _allocation_items, _cents, _decimal, _plain
from purser.mcp.toolschemas import (
    CaptureAwardInput,
    CapturePositionInput,
    CaptureResult,
    ListAwardsResult,
    ListLotsResult,
    LotAllocationInput,
    LotChangeResult,
    RealizedGainsResult,
    RecordVestResult,
    TransferOutResult,
    VestStatusResult,
)
from purser.privacy import NOTHING, Emits

_BUDGET = 1900  # characters of structuredContent JSON (< 500 tokens)
_FIRST = 5  # changes / conflicts itemized in a capture answer


def _date(raw: str, field: str) -> date_type:
    try:
        return date_type.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {raw!r}") from exc


def _instant(raw: str) -> datetime:
    """captured_at is required: it is part of the request a commit must
    repeat exactly (a default of "now" would make every call a new one)."""
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"captured_at must be an ISO instant, got {raw!r}") from exc
    if value.tzinfo is None:
        raise ValueError("captured_at needs a time zone (e.g. 2026-11-15T14:00:00Z)")
    return value


def _required(raw: str | None, field: str) -> Decimal:
    value = _decimal(raw, field)
    if value is None:
        raise ValueError(f"{field} is required")
    return value


def _fits(payload: dict[str, Any]) -> bool:
    return len(json.dumps(payload)) <= _BUDGET


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@audited_tool(
    kind="read",
    entity="lot",
    scope="read",
    emits=Emits(
        external_ids=("lots[].external_ref",),
        description=("lots[].plan",),
        # A lot's basis is a single amount: its per-share figure times its
        # remaining shares gives it away, so both go together.
        amount=("lots[].remaining_basis", "lots[].basis_per_share"),
    ),
)
async def list_lots(
    account: str,
    symbol: str,
    status: str = "open",
    cursor: str | None = None,
) -> Annotated[CallToolResult, ListLotsResult]:
    """A position's tax lots, on an account whose holdings are entered in
    Purser (read scope), oldest first, a page at a time, with its lot
    summary: shares held, shares in lots, unallocated shares (outside every
    lot), shares whose lot has no basis,
    and cost_basis — the lots' basis, null unless every share has one (never
    an average). capture says when the broker's lots were last captured
    (as_of, captured_at, completeness): capture freshness, distinct from
    price_as_of, the quote's date. Each lot: lot_id (to name it in
    record_sale, record_transfer_out or void_lot), acquired_on, acquisition,
    status, remaining, basis_per_share (null: unknown), and term_if_sold_today
    (long only after the one-year anniversary). status is open (default),
    closed, voided, reversed or all. next_cursor pages."""

    def op(session: Session) -> CallToolResult:
        from purser import appsettings
        from purser import ledger as ledger_service
        from purser.core import lots as core_lots
        from purser.holdings import lots as lot_service
        from purser.holdings import service as holdings_service

        today = clock.ledger_today()
        acct = ledger_service.resolve_account(session, account)
        normalized = holdings_service.symbol_of(symbol)
        position = holdings_service.open_position(session, acct.id, normalized)
        if position is None:
            raise ValueError(f"{acct.path} has no open {normalized} position")
        page = lot_service.list_lots(
            session, position.id, status=status, limit=lot_service.MAX_PAGE, cursor=cursor
        )
        state = holdings_service.position_state(
            session, position, stale_days=appsettings.effective().price_stale_days
        )
        s = page.summary
        summary: dict[str, Any] = {
            "position_id": str(position.id),
            "symbol": position.symbol,
            "quantity": _plain(s.quantity),
            "tracks_lots": s.tracks_lots,
            "open_lots": s.open_lots,
            "lots_quantity": _plain(s.lots_quantity),
            "unallocated": _plain(s.unallocated),
            "basis_unknown_quantity": _plain(s.basis_unknown_quantity),
            "cost_basis": _cents(s.cost_basis),
            "capture": (
                {
                    "as_of": s.freshness.as_of.isoformat(),
                    "captured_at": s.freshness.captured_at,
                    "completeness": s.freshness.completeness,
                }
                if s.freshness
                else None
            ),
            "price_as_of": (state.price.price_date.isoformat() if state and state.price else None),
        }
        start = int(cursor) if cursor else 0
        lines: list[dict[str, Any]] = []
        payload: dict[str, Any] = {
            "summary": summary,
            "lots": lines,
            "total": page.total,
            "next_cursor": None,
        }
        for i, view in enumerate(page.lots):
            lot = view.lot
            line: dict[str, Any] = {
                "lot_id": str(lot.id),
                "acquired_on": lot.acquired_on.isoformat(),
                "acquisition": lot.acquisition,
                "status": view.status,
                "remaining": _plain(view.remaining),
                "basis_per_share": (
                    _plain(Decimal(lot.basis_per_share))
                    if lot.basis_per_share is not None
                    else None
                ),
                "term_if_sold_today": core_lots.term(lot.acquired_on, today),
                "plan": _short(lot.plan),
                "external_ref": _short(lot.external_ref),
                "remaining_basis": _cents(view.remaining_basis),
            }
            if view.award_vest_id is not None:
                line["vest_linked"] = True
            lines.append(line)
            if not _fits(payload) and len(lines) > 1:
                lines.pop()
                payload["next_cursor"] = str(start + i)
                break
        else:
            if page.next_cursor is not None:
                payload["next_cursor"] = str(start + len(page.lots))
        shown = len(lines)
        headline = (
            f"{acct.path} {position.symbol}: {summary['quantity']} shares, "
            f"{s.open_lots} open lot(s) holding {summary['lots_quantity']}"
            + (f", {summary['unallocated']} unallocated" if s.unallocated else "")
            + (
                f", {summary['basis_unknown_quantity']} in lots with no basis"
                if s.basis_unknown_quantity
                else ""
            )
            + "."
            + (
                f" Lots last captured as of {summary['capture']['as_of']}."
                if summary["capture"]
                else " No capture of this position's lots yet."
            )
            + (
                f" This page lists {shown} of {page.total}; call again with cursor "
                f"{payload['next_cursor']!r}."
                if payload["next_cursor"]
                else ""
            )
        )
        return presentation.three_tier(headline, payload)

    return await _run(op)


@audited_tool(
    kind="read",
    entity="award",
    scope="read",
    emits=Emits(external_ids=("awards[].grant_ref",), description=("awards[].plan",)),
)
async def list_awards(
    account: str,
    symbol: str | None = None,
    award: str | None = None,
    cursor: str | None = None,
) -> Annotated[CallToolResult, ListAwardsResult]:
    """Equity awards on a brokerage account (read scope), a page at a time:
    each award (award_id, granted, cancelled, vested, unvested,
    unvested_known, unscheduled — unvested units with no vest date yet — the
    next vest, due_unrecorded: scheduled vests on or before today not yet
    recorded with record_vest), and on the first page the unvested units per
    symbol, valued at the latest close — informational: unvested units are
    never in net worth, balances or holdings. Pass award (an award_id, or
    the grant reference) for that award's vest schedule (vest_id, date,
    units, status), paged the same way — the vest_id is what
    update_award_vest takes. unvested_known false means the broker gave no
    vested count and the schedule does not account for every unit: the
    figure is the scheduled units only, not the whole balance. capture says
    when the broker's awards were last captured. next_cursor continues the
    same listing (same account, symbol and award)."""

    def op(session: Session) -> CallToolResult:
        from sqlalchemy import select

        from purser import ledger as ledger_service
        from purser.holdings import awards as award_service
        from purser.models import HoldingsCapture

        acct = ledger_service.resolve_account(session, account)
        views = award_service.award_views(session, acct, symbol=symbol)
        target = None
        if award is not None:
            target = award_service.resolve_award(session, award=award, account=account)
            views = [v for v in views if v.award.id == target.id]
        try:
            start = int(cursor) if cursor else 0
        except ValueError as exc:
            raise ValueError("cursor is the next_cursor of a previous call") from exc
        if start < 0:
            raise ValueError("cursor is the next_cursor of a previous call")
        cap = session.execute(
            select(HoldingsCapture)
            .where(HoldingsCapture.account_id == acct.id, HoldingsCapture.kind == "awards")
            .order_by(HoldingsCapture.as_of.desc(), HoldingsCapture.captured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        unvested = award_service.unvested(session, acct) if start == 0 else []
        if target is not None or symbol is not None:
            wanted = {v.award.symbol for v in views}
            unvested = [u for u in unvested if u.symbol in wanted]
        lines: list[dict[str, Any]] = []
        payload: dict[str, Any] = {
            "unvested": [],
            "unvested_truncated": False,
            "awards": lines,
            "total": 0,
            "next_cursor": None,
            "capture": (
                {
                    "as_of": cap.as_of.isoformat(),
                    "captured_at": cap.captured_at.isoformat(),
                    "completeness": cap.completeness,
                }
                if cap
                else None
            ),
            "note": "Unvested units are not owned: never in net worth or holdings.",
        }

        def award_line(v: Any) -> dict[str, Any]:
            a = v.award
            return {
                "award_id": str(a.id),
                "symbol": a.symbol,
                "granted_on": a.granted_on.isoformat(),
                "granted": _plain(Decimal(a.granted_quantity)),
                "cancelled": _plain(Decimal(a.cancelled_quantity)),
                "vested": _plain(v.vested),
                "unvested": _plain(v.unvested),
                "unvested_known": v.unvested_known,
                "unscheduled": _plain(v.unscheduled) if v.unscheduled is not None else None,
                "next_vest_date": v.next_vest.vest_date.isoformat() if v.next_vest else None,
                "next_vest_units": _plain(Decimal(v.next_vest.quantity)) if v.next_vest else None,
                "due_unrecorded": v.due_unrecorded,
                "grant_ref": _short(a.grant_ref),
                "plan": _short(a.plan),
            }

        # The per-symbol summary first, as many symbols as fit half the
        # budget (largest first), so a page always has room for its items.
        for u in sorted(unvested, key=lambda u: (-u.units, u.symbol)):
            payload["unvested"].append(
                {
                    "symbol": u.symbol,
                    "units": _plain(u.units),
                    "value": _cents(u.value),
                    "price_as_of": u.price.price_date.isoformat() if u.price else None,
                    "next_vest_date": u.next_vest_date.isoformat() if u.next_vest_date else None,
                    "next_vest_units": (
                        _plain(u.next_vest_units) if u.next_vest_units is not None else None
                    ),
                    "unscheduled": _plain(u.unscheduled),
                    "complete": u.complete,
                }
            )
            if len(json.dumps(payload)) > _BUDGET // 2:
                payload["unvested"].pop()
                payload["unvested_truncated"] = True
                break

        if target is not None and views:
            # One award: its line, then its schedule, paged.
            lines.append(award_line(views[0]))
            schedule = views[0].vests
            vest_lines: list[dict[str, Any]] = []
            payload["vests"] = vest_lines
            payload["total"] = len(schedule)
            items: list[Any] = schedule
        else:
            payload["total"] = len(views)
            items = views
        for i, item in enumerate(items[start:]):
            if target is not None:
                payload["vests"].append(
                    {
                        "vest_id": str(item.id),
                        "vest_date": item.vest_date.isoformat(),
                        "ordinal": item.ordinal,
                        "quantity": _plain(Decimal(item.quantity)),
                        "status": item.status,
                        "lot_id": str(item.lot_id) if item.lot_id else None,
                    }
                )
                added = payload["vests"]
            else:
                lines.append(award_line(item))
                added = lines
            if not _fits(payload) and len(added) > 1:
                added.pop()
                payload["next_cursor"] = str(start + i)
                break

        units = [u for u in unvested]
        headline = f"{acct.path}: {len(views)} award(s)"
        if units:
            headline += "; " + ", ".join(
                f"{_plain(u.units)} {u.symbol} unvested"
                + (
                    f" (next {_plain(u.next_vest_units)} on {u.next_vest_date.isoformat()})"
                    if u.next_vest_date and u.next_vest_units is not None
                    else ""
                )
                for u in units[:3]
            )
            if len(units) > 3:
                headline += f" and {len(units) - 3} more symbol(s)"
        headline += ". Not owned until vested: never in net worth."
        unscheduled = sum((u.unscheduled for u in units), Decimal(0))
        if unscheduled:
            headline += f" {_plain(unscheduled)} unvested unit(s) have no vest date yet."
        if any(not u.complete for u in units):
            headline += (
                " Some awards' remaining units are unknown (no vested count from the broker): "
                "only their scheduled units are counted."
            )
        due = sum(v.due_unrecorded for v in views)
        if due:
            headline += f" {due} scheduled vest(s) are due and not recorded (record_vest)."
        if payload["next_cursor"]:
            what = "vests" if target is not None else "awards"
            shown = len(payload["vests"]) if target is not None else len(lines)
            headline += (
                f" This page lists {shown} of {payload['total']} {what} from #{start + 1}; call "
                f"again with cursor {payload['next_cursor']!r} for more."
            )
        return presentation.three_tier(headline, payload)

    return await _run(op)


def _short(text: str | None, limit: int = 60) -> str | None:
    """A label shortened for a budgeted answer (the full one is on REST)."""
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@audited_tool(kind="read", entity="lot", scope="read", emits=NOTHING)
async def realized_gains(
    account: str, year: int, symbol: str | None = None
) -> Annotated[CallToolResult, RealizedGainsResult]:
    """Realized gains of an account's lot dispositions in a calendar year
    (read scope), by term: short, or long (sold after the one-year
    anniversary). Totals sum only dispositions with both a basis and
    proceeds; shares lacking either are counted (unknown_basis_quantity,
    unknown_proceeds_quantity), never valued as 0, and shares sold from
    outside every lot are unallocated_quantity. Informational: Purser
    computes no tax."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import lots as lot_service

        r = lot_service.realized_gains(session, account_ref=account, year=year, symbol=symbol)

        def term(t: Any) -> dict[str, Any]:
            return {
                "disposals": t.disposals,
                "quantity": _plain(t.quantity),
                "proceeds": _cents(Decimal(t.proceeds)) or "0.00",
                "cost_basis": _cents(Decimal(t.cost_basis)) or "0.00",
                "realized_gain": _cents(Decimal(t.realized_gain)) or "0.00",
                "unknown_basis_quantity": _plain(t.unknown_basis_quantity),
                "unknown_proceeds_quantity": _plain(t.unknown_proceeds_quantity),
            }

        payload: dict[str, Any] = {
            "account": r.account.path,
            "year": year,
            "symbol": r.symbol,
            "short": term(r.by_term["short"]),
            "long": term(r.by_term["long"]),
            "unallocated_quantity": _plain(r.unallocated_quantity),
            "note": "Informational: Purser computes no tax.",
        }
        headline = (
            f"{r.account.path} {year}: short-term gain {payload['short']['realized_gain']} "
            f"over {payload['short']['disposals']} disposal(s), long-term "
            f"{payload['long']['realized_gain']} over {payload['long']['disposals']}."
        )
        unknown = (
            r.by_term["short"].unknown_basis_quantity + r.by_term["long"].unknown_basis_quantity
        )
        if unknown:
            headline += f" {_plain(unknown)} share(s) sold from lots with no basis are not in it."
        return presentation.three_tier(headline, payload)

    return await _run(op)


# --------------------------------------------------------------------------
# Captures
# --------------------------------------------------------------------------


def _capture_answer(
    *,
    dry_run: bool,
    applied: bool,
    retry: bool,
    repeat_of: uuid.UUID | None,
    plan_hash: str,
    capture_id: uuid.UUID | None,
    changes: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    unchanged: int,
    stale: list[str],
    skipped: list[str],
    what: str,
) -> CallToolResult:
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "applied": applied,
        "retry": retry,
        "repeat_of": str(repeat_of) if repeat_of else None,
        "plan_hash": plan_hash,
        "capture_id": str(capture_id) if capture_id else None,
        "counts": counts,
        "conflicts": len(conflicts),
        "first_changes": changes[:_FIRST],
        "first_conflicts": conflicts[:_FIRST],
        "unchanged": unchanged,
        "stale_symbols": stale,
        "skipped_symbols": skipped,
    }
    while not _fits(payload) and (payload["first_changes"] or payload["first_conflicts"]):
        key = "first_changes" if payload["first_changes"] else "first_conflicts"
        payload[key] = payload[key][:-1]
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no changes"
    if retry:
        headline = (
            f"This exact {what} capture was already applied (capture {capture_id}); "
            "nothing written."
        )
    elif dry_run:
        headline = f"Dry run, nothing written: {summary}; {len(conflicts)} conflict(s)."
        if conflicts:
            headline += (
                " Conflicts are never applied: resolve them (record the sale or transfer out, "
                "void a wrong lot, forfeit a vest) and capture again, or commit with "
                "skip_conflicts to apply what has none."
            )
        headline += f" To apply: call again with dry_run=false and expect_hash={plan_hash!r}."
    else:
        headline = f"Applied {what} capture {capture_id}: {summary}."
        if repeat_of:
            headline += " Same holdings as the previous capture: freshness recorded only."
        if skipped:
            headline += f" Skipped for conflicts: {', '.join(skipped)}."
    if stale:
        headline += (
            f" Older than a capture already applied ({', '.join(stale)}): compared, not applied."
        )
    return presentation.three_tier(headline, payload)


def _capture_error(exc: Exception) -> Exception:
    from purser.holdings import awards as award_service
    from purser.holdings import captures
    from purser.ledger import LedgerError

    if isinstance(exc, captures.PreviewStale | award_service.AwardPreviewStale):
        return LedgerError(
            f"preview_stale: {exc} New plan_hash: {exc.preview.plan_hash} (dry-run again to "
            "review it)."
        )
    if isinstance(exc, captures.CaptureConflicts | award_service.AwardCaptureConflicts):
        return LedgerError(f"capture_conflicts: {exc}")
    return exc


@audited_tool(kind="write", entity="lot", scope="write", emits=NOTHING)
async def upsert_lots(
    account: str,
    as_of: str,
    completeness: str,
    positions: list[CapturePositionInput],
    captured_at: str,
    source_note: str | None = None,
    basis_policy: str = "keep",
    dry_run: bool = True,
    expect_hash: str | None = None,
    skip_conflicts: bool = False,
) -> Annotated[CallToolResult, CaptureResult]:
    """Bulk, idempotent input of a broker's tax lots as of a day (write
    scope) on a brokerage account whose holdings are entered in Purser. positions: [{symbol, lots:
    [{acquired_on, quantity (shares REMAINING on as_of), external_ref (the
    broker's lot reference, when shown: the lot's identity), plan,
    ordinal (when lots of one date and plan have no reference: 1, 2, ...
    in a stable order), basis_per_share and/or cost_basis (of the remaining
    shares; omit when unknown — never 0), acquisition (buy, rsu_vest,
    plan_purchase, espp_purchase, transfer_in, drip, gift, unknown)}]}].
    Decimal strings; dates YYYY-MM-DD. completeness: complete (each listed
    symbol's list is the broker's whole list) or partial (only these rows;
    nothing is inferred from what is missing). captured_at: when you read
    the page (ISO instant with zone; send the same value on the commit);
    source_note: where (no personal data).

    dry_run=true (the default) returns the plan and plan_hash, writing
    nothing; commit with dry_run=false and expect_hash=plan_hash (refused
    with preview_stale if anything changed). Re-sending the exact same
    request is a no-op (retry); the same holdings read again later only
    record freshness. New lots are adopted; a complete capture recounts
    the position up to the broker's total. Never applied, always
    conflicts: a lot with fewer remaining shares (record the sale or
    transfer out naming that lot first), more shares, a lot missing from a
    complete list, a differing basis (basis_policy='replace' takes the
    broker's when nothing drew on the lot), an ambiguous identity, a
    partial capture whose new lots exceed the unallocated shares, or a
    capture older than one applied. skip_conflicts=true applies the
    symbols without any."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import captures

        principal = _principal()
        capture = captures.LotCaptureIn(
            account=account,
            as_of=_date(as_of, "as_of"),
            captured_at=_instant(captured_at),
            completeness=completeness,
            source_note=source_note,
            basis_policy=basis_policy,
            positions=tuple(
                captures.SymbolIn(
                    symbol=p["symbol"],
                    name=p.get("name"),
                    lots=tuple(
                        captures.LotIn(
                            acquired_on=_date(r["acquired_on"], "acquired_on"),
                            quantity=_required(r.get("quantity"), "quantity"),
                            external_ref=r.get("external_ref"),
                            plan=r.get("plan"),
                            ordinal=r.get("ordinal"),
                            basis_per_share=_decimal(r.get("basis_per_share"), "basis_per_share"),
                            cost_basis=_decimal(r.get("cost_basis"), "cost_basis"),
                            acquisition=r.get("acquisition") or "unknown",
                        )
                        for r in p["lots"]
                    ),
                )
                for p in positions
            ),
        )
        try:
            if dry_run:
                preview = captures.preview(session, capture)
                applied = None
            else:
                if not expect_hash:
                    raise ValueError("a commit needs expect_hash: the plan_hash of a dry run")
                applied = captures.commit(
                    session,
                    capture,
                    expect_hash=expect_hash,
                    skip_conflicts=skip_conflicts,
                    token=_token_row(session, principal),
                )
                preview = applied.preview
        except Exception as exc:
            raise _capture_error(exc) from exc
        changes = [
            {
                "kind": c.kind,
                "symbol": s.symbol,
                "row": c.row,
                "lot_id": str(c.lot_id) if c.lot_id else None,
                "quantity": _plain(c.quantity) if c.quantity is not None else None,
            }
            for s in preview.symbols
            for c in s.plan.changes
        ]
        conflicts = [
            {
                "kind": c.code,
                "symbol": s.symbol,
                "row": c.row,
                "lot_id": str(c.lot_id) if c.lot_id else None,
                "message": c.message,
            }
            for s in preview.symbols
            for c in s.plan.conflicts
        ]
        if dry_run:
            session.rollback()
        return _capture_answer(
            dry_run=dry_run,
            applied=applied is not None and not applied.retry,
            retry=(applied.retry if applied else preview.already_applied is not None),
            repeat_of=(applied.capture.repeat_of if applied else preview.repeat_of),
            plan_hash=preview.plan_hash,
            capture_id=(
                applied.capture.id
                if applied
                else (preview.already_applied.id if preview.already_applied else None)
            ),
            changes=changes,
            conflicts=conflicts,
            unchanged=sum(s.plan.unchanged for s in preview.symbols),
            stale=[s.symbol for s in preview.symbols if s.stale],
            skipped=list(applied.skipped_symbols) if applied else [],
            what="lot",
        )

    return await _run(op)


@audited_tool(kind="write", entity="award", scope="write", emits=NOTHING)
async def upsert_awards(
    account: str,
    as_of: str,
    completeness: str,
    awards: list[CaptureAwardInput],
    captured_at: str,
    source_note: str | None = None,
    dry_run: bool = True,
    expect_hash: str | None = None,
    skip_conflicts: bool = False,
) -> Annotated[CallToolResult, CaptureResult]:
    """Bulk, idempotent input of equity awards and their vest schedules
    (write scope): awards [{grant_ref (the broker's grant id: the award's
    identity), symbol, granted_on, granted_quantity, cancelled_quantity,
    vested_quantity (as the broker reports it), plan, schedule: [{vest_date,
    quantity, ordinal (only for two vests on one date)}] — the vests not
    yet delivered}]. Same contract as upsert_lots: dry_run=true by default,
    commit with expect_hash, exact retries are no-ops, conflicts are never
    applied. Awards never touch holdings or net worth: a vest becomes shares
    only through record_vest. A re-estimated scheduled quantity, new vests,
    more cancelled units and a new reported vested count are applied; a
    changed grant size, fewer cancelled units, or (complete) a scheduled
    future vest the broker no longer lists is a conflict — forfeit it with
    update_award_vest if it is gone. A scheduled vest on or before as_of
    that the broker no longer lists is counted as due: record it."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import awards as award_service

        principal = _principal()
        capture = award_service.AwardCaptureIn(
            account=account,
            as_of=_date(as_of, "as_of"),
            captured_at=_instant(captured_at),
            completeness=completeness,
            source_note=source_note,
            awards=tuple(
                award_service.AwardIn(
                    grant_ref=a["grant_ref"],
                    symbol=a["symbol"],
                    granted_on=_date(a["granted_on"], "granted_on"),
                    granted_quantity=_required(a.get("granted_quantity"), "granted_quantity"),
                    cancelled_quantity=_decimal(a.get("cancelled_quantity"), "cancelled_quantity"),
                    vested_quantity=_decimal(a.get("vested_quantity"), "vested_quantity"),
                    plan=a.get("plan"),
                    schedule=tuple(
                        award_service.VestIn(
                            _date(v["vest_date"], "vest_date"),
                            _required(v.get("quantity"), "quantity"),
                            v.get("ordinal"),
                        )
                        for v in a.get("schedule", [])
                    ),
                )
                for a in awards
            ),
        )
        try:
            if dry_run:
                preview = award_service.preview_awards(session, capture)
                applied = None
            else:
                if not expect_hash:
                    raise ValueError("a commit needs expect_hash: the plan_hash of a dry run")
                applied = award_service.commit_awards(
                    session,
                    capture,
                    expect_hash=expect_hash,
                    skip_conflicts=skip_conflicts,
                    token=_token_row(session, principal),
                )
                preview = applied.preview
        except Exception as exc:
            raise _capture_error(exc) from exc
        changes = [
            {
                "kind": c.kind,
                "row": c.award,
                "quantity": _plain(c.quantity) if c.quantity is not None else None,
            }
            for c in preview.plan.changes
        ]
        conflicts = [
            {"kind": c.code, "row": c.award, "message": c.message} for c in preview.plan.conflicts
        ]
        if dry_run:
            session.rollback()
        return _capture_answer(
            dry_run=dry_run,
            applied=applied is not None and not applied.retry,
            retry=(applied.retry if applied else preview.already_applied is not None),
            repeat_of=(applied.capture.repeat_of if applied else preview.repeat_of),
            plan_hash=preview.plan_hash,
            capture_id=(
                applied.capture.id
                if applied
                else (preview.already_applied.id if preview.already_applied else None)
            ),
            changes=changes,
            conflicts=conflicts,
            unchanged=preview.plan.unchanged,
            stale=["awards"] if preview.stale else [],
            skipped=[],
            what="award",
        )

    return await _run(op)


# --------------------------------------------------------------------------
# Single writes
# --------------------------------------------------------------------------


@audited_tool(kind="write", entity="award", scope="write", emits=NOTHING)
async def record_vest(
    account: str,
    award: str,
    vest_date: str,
    gross_quantity: str,
    withheld_quantity: str,
    fmv: str | None = None,
    lot: str | None = None,
    lot_ref: str | None = None,
    force_new_lot: bool = False,
    ordinal: int | None = None,
    note: str | None = None,
    dry_run: bool = False,
) -> Annotated[CallToolResult, RecordVestResult]:
    """Record an award vest (write scope): award is the award_id or the
    broker's grant reference; gross_quantity the units that vested,
    withheld_quantity is REQUIRED: units withheld for tax, explicitly "0"
    only when known (net = gross - withheld). Never infer it from sold/withdrawn
    totals; leave the historical vest unrecorded until evidence is available. fmv
    the fair market value per share at vest (withheld = gross records a
    vest that delivered no shares: no event, no lot). By default a vest event adds
    the net shares to the position and opens their lot (basis = fmv;
    lot_ref = the broker's lot reference, if shown). If the broker's lot
    list was already captured, the vest's lot is already there: pass lot
    (its lot_id or reference) to LINK it instead — no shares are added;
    the lot must be acquired on vest_date with the net shares (fewer if
    some were sold before the capture) and fmv as its basis. With an
    unlinked lot acquired that day and neither lot nor force_new_lot, the
    call is refused (possible_duplicate) so a vest is never counted twice.
    Recording the same vest again with the same figures is a no-op
    (already_recorded); with other figures it is refused — reverse the vest
    event (reverse_position_event) first. dry_run=true writes nothing."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import awards as award_service

        principal = _principal()
        try:
            out = award_service.record_vest(
                session,
                award=award,
                account=account,
                vest_date=_date(vest_date, "vest_date"),
                ordinal=ordinal,
                gross_quantity=_required(gross_quantity, "gross_quantity"),
                withheld_quantity=_required(withheld_quantity, "withheld_quantity"),
                fmv=_decimal(fmv, "fmv"),
                lot=lot,
                lot_ref=lot_ref,
                force_new_lot=force_new_lot,
                note=note,
                token=_token_row(session, principal),
                dry_run=dry_run,
            )
        except award_service.VestConflict as exc:
            from purser.ledger import LedgerError

            raise LedgerError(f"vest_conflict: {exc}") from exc
        remaining = None
        if not dry_run:
            for u in award_service.unvested(session, _account(session, out.award)):
                if u.symbol == out.award.symbol:
                    remaining = _plain(u.units)
        payload = {
            "award_id": str(out.award.id),
            "vest_id": str(out.vest.id) if out.vest is not None else None,
            "lot_id": str(out.lot_id) if out.lot_id else None,
            "event_id": str(out.event_id) if out.event_id else None,
            "position_id": str(out.position_id) if out.position_id else None,
            "linked": out.linked,
            "already_recorded": out.already_recorded,
            "dry_run": out.dry_run,
            "gross": _plain(out.gross),
            "withheld_units": _plain(out.withheld),
            "net": _plain(out.net),
            "unvested_after": remaining,
        }
        if out.already_recorded:
            headline = "This vest is already recorded with these figures; nothing written."
        elif out.net == 0 and not out.dry_run:
            headline = (
                f"Recorded the {vest_date} vest of {out.award.symbol}: every unit was withheld, "
                "so no shares and no lot"
                + (f"; {remaining} units still unvested." if remaining else ".")
            )
        elif out.dry_run:
            headline = (
                "Dry run, nothing written: the vest would "
                + ("link the existing lot" if out.linked else f"add {_plain(out.net)} shares")
                + "."
            )
        else:
            headline = (
                f"Recorded the {vest_date} vest of {out.award.symbol}: "
                + (
                    "linked the existing lot (no shares added)"
                    if out.linked
                    else f"{_plain(out.net)} net shares in a new lot"
                )
                + (f"; {remaining} units still unvested." if remaining else ".")
            )
        return presentation.three_tier(headline, payload)

    return await _run(op)


def _account(session: Session, award: Any) -> Any:
    from purser import ledger as ledger_service

    return ledger_service.get_account(session, award.account_id)


@audited_tool(kind="write", entity="position", scope="write", emits=NOTHING)
async def record_transfer_out(
    account: str,
    symbol: str,
    quantity: str,
    occurred_on: str,
    lots: list[LotAllocationInput] | None = None,
    lot_method: str | None = None,
    note: str | None = None,
) -> Annotated[CallToolResult, TransferOutResult]:
    """Record shares that left a position entered in Purser with no price and no
    cash (write scope): a transfer to another broker, or a decrease the
    broker shows that is not a sale you can price. Never use it for a sale
    (record_sale). On a position that tracks lots, name the lots: lots
    [{lot, quantity}] (lot_id, the broker's reference, or 'unallocated') or
    lot_method fifo/lifo/hifo. Undo with reverse_position_event."""

    def op(session: Session) -> CallToolResult:
        from purser import ledger as ledger_service
        from purser.holdings import service as svc

        principal = _principal()
        acct = ledger_service.resolve_account(session, account)
        position = svc.open_position(session, acct.id, svc.symbol_of(symbol))
        if position is None:
            raise ValueError(f"{acct.path} has no open {symbol} position")
        allocation = svc.resolve_allocation(
            session, position.id, method=lot_method, items=_allocation_items(lots)
        )
        written = svc._record_event(
            session,
            position_id=position.id,
            kind="transfer_out",
            occurred_on=_date(occurred_on, "occurred_on"),
            quantity=_required(quantity, "quantity"),
            note=note,
            token=_token_row(session, principal),
            allocation=allocation,
        )
        held = svc.quantity_on(session, position, clock.ledger_today())
        payload = {
            "position_id": str(position.id),
            "symbol": position.symbol,
            "event_id": str(written.event.id),
            "quantity_after": _plain(held),
            "lots": len([d for d in written.dispositions if d.lot_id is not None]),
        }
        return presentation.three_tier(
            f"Recorded {quantity} {position.symbol} leaving {acct.path} on {occurred_on} (no "
            f"cash); {_plain(held)} shares held today.",
            payload,
        )

    return await _run(op)


def _lot_answer(session: Session, lot_id: uuid.UUID, verb: str) -> CallToolResult:
    from purser.holdings import lots as lot_service

    detail = lot_service.lot_detail(session, lot_id)
    lot = detail.view.lot
    payload = {
        "lot_id": str(lot.id),
        "position_id": str(lot.position_id),
        "status": detail.view.status,
        "remaining": _plain(detail.view.remaining),
        "basis_per_share": (
            _plain(Decimal(lot.basis_per_share)) if lot.basis_per_share is not None else None
        ),
        "cost_basis_known": lot.cost_basis is not None,
    }
    return presentation.three_tier(f"Lot {lot.id} {verb}.", payload)


@audited_tool(kind="write", entity="lot", scope="write", emits=NOTHING)
async def void_lot(
    lot_id: str, note: str | None = None
) -> Annotated[CallToolResult, LotChangeResult]:
    """Void a lot that a capture adopted wrongly (write scope): it stops
    counting and stays as read-only history. Refused while a sale or
    transfer out draws on it (reverse those first with
    reverse_position_event, then record them again against the right
    lots), while an award vest is linked to it, and for a lot opened by an
    event (reverse that event instead). Then capture again (a new
    captured_at: the exact same request is a no-op) to record the right
    lot."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import lots as lot_service

        principal = _principal()
        target = uuid.UUID(lot_id)
        lot_service.void_lot(
            session, lot_id=target, note=note, token=_token_row(session, principal)
        )
        return _lot_answer(session, target, "voided")

    return await _run(op)


@audited_tool(kind="write", entity="lot", scope="write", emits=NOTHING)
async def correct_lot_basis(
    lot_id: str,
    basis_per_share: str | None = None,
    cost_basis: str | None = None,
    note: str | None = None,
) -> Annotated[CallToolResult, LotChangeResult]:
    """Set or correct a lot's basis (write scope): per share, total for the
    lot's quantity, or both agreeing; neither sets it back to unknown.
    Refused while a disposition drew on the lot (its basis came from it):
    reverse those, correct, record them again."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import lots as lot_service

        principal = _principal()
        target = uuid.UUID(lot_id)
        lot_service.correct_basis(
            session,
            lot_id=target,
            basis_per_share=_decimal(basis_per_share, "basis_per_share"),
            cost_basis=_decimal(cost_basis, "cost_basis"),
            note=note,
            token=_token_row(session, principal),
        )
        return _lot_answer(session, target, "basis corrected")

    return await _run(op)


@audited_tool(kind="write", entity="award", scope="write", emits=NOTHING)
async def update_award_vest(
    vest_id: str, status: str, note: str | None = None
) -> Annotated[CallToolResult, VestStatusResult]:
    """Forfeit a scheduled vest (status 'forfeited'), reinstate a forfeited
    one, or set a vest that was LINKED to an existing lot back to
    'scheduled' (write scope). A vest recorded as an event goes back by
    reversing that event (reverse_position_event). vest_id comes from
    list_awards(award=...)."""

    def op(session: Session) -> CallToolResult:
        from purser.holdings import awards as award_service

        principal = _principal()
        vest = award_service.update_vest(
            session,
            vest_id=uuid.UUID(vest_id),
            status=status,
            note=note,
            token=_token_row(session, principal),
        )
        payload = {"vest_id": str(vest.id), "award_id": str(vest.award_id), "status": vest.status}
        return presentation.three_tier(
            f"Vest {vest.id} on {vest.vest_date.isoformat()} is now {vest.status}.", payload
        )

    return await _run(op)
