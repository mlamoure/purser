"""whats_new: the agent-briefing digest over the events stream (issue #77).

One call compacts everything since the caller's cursor into pre-summarized
sections — counts and totals for everything, itemized lines only for what
clears the materiality bar (plus every variance breach, shortfall, and
price change). Pages the same durable events table as webhook replay, with
the same (at, id) keyset cursor, so consecutive cursor-chained calls never
repeat nor drop an event.

The primary rendering is the agent's own narration; the digest widget is a
compact receipt.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from purser import webhooks
from purser.models import Event, Match, Payee, Posting, Schedule, Transaction

DEFAULT_MATERIALITY = Decimal("50")
SCAN_LIMIT = 500  # events examined per call; events_truncated resumes exactly
DEFAULT_WINDOW_DAYS = 7  # first call (no cursor) looks back this far

NOTABLE_CAP = 3
MATCHES_CAP = 3
PRICE_CHANGES_CAP = 3
FORECAST_FLAGS_CAP = 2
BALANCE_DRIFTS_CAP = 3
UPCOMING_CAP = 4
PENDINGS_VANISHED_CAP = 3
STALE_PRICES_CAP = 3
UNPRICED_HOLDINGS_CAP = 3
OPEN_PERIODS_CAP = 3


def _drift_line(event: Event) -> dict[str, Any]:
    """One ``balance_drifts`` entry. ``basis`` is the ledger basis the delta is
    measured on (the event's ``mode``: posted_only when the bank's balance
    leaves pendings out, decision 64), present only when the event recorded
    one; ``pinned: true`` only when that basis is the account's pin rather
    than learned (the event's ``basis_pinned``, decision 83) — guardrail 6."""
    line: dict[str, Any] = {
        "account": event.payload.get("account") or str(event.entity_id),
        "delta": event.payload.get("delta", "0"),
        "balance_date": event.payload.get("balance_date"),
    }
    basis = event.payload.get("mode")
    if isinstance(basis, str) and basis:
        line["basis"] = basis
        if event.payload.get("basis_pinned") is True:
            line["pinned"] = True
    return line


def _real_leg(tx: Transaction) -> Posting | None:
    real = [p for p in tx.postings if not p.account.is_virtual]
    return real[0] if len(real) == 1 else None


def _tx_lines(session: Session, event_rows: list[Event], materiality: Decimal) -> dict[str, Any]:
    ids = [e.entity_id for e in event_rows if e.entity_id is not None]
    txs = (
        {
            t.id: t
            for t in session.execute(select(Transaction).where(Transaction.id.in_(ids))).scalars()
        }
        if ids
        else {}
    )
    count = 0
    total = Decimal(0)
    notable: list[dict[str, Any]] = []
    for event in event_rows:
        tx = txs.get(event.entity_id) if event.entity_id else None
        if tx is None:
            count += 1
            continue
        count += 1
        leg = _real_leg(tx)
        amount = Decimal(leg.amount) if leg is not None else Decimal(0)
        total += abs(amount)
        # voided/superseded rows still count, but they aren't notable lines
        if tx.status == "void" or tx.superseded_by is not None:
            continue
        if abs(amount) > materiality:
            notable.append(
                {
                    "payee": tx.payee.name if tx.payee else None,
                    # A payee-less row is labelled by its description, in its
                    # own key so minimal disclosure can withhold it (#437).
                    **({"description": tx.description} if not tx.payee else {}),
                    "amount": str(amount),
                    "transaction_id": str(tx.id),
                }
            )
    return {
        "count": count,
        "total": str(total.quantize(Decimal("0.01"))),
        "notable": notable[:NOTABLE_CAP],
        "truncated": len(notable) > NOTABLE_CAP,
    }


def _vanished_lines(
    session: Session, event_rows: list[Event], materiality: Decimal
) -> dict[str, Any]:
    """Pending rows the feed voided with no posted successor (``pending.vanished``,
    #282): the bank dropped them. A dropped hold is routine; a dropped refund or
    a charge that later posts under another amount is worth a look, so every one
    is counted and those above the materiality bar are itemized, largest first.
    """
    ids = [e.entity_id for e in event_rows if e.entity_id is not None]
    txs = (
        {
            t.id: t
            for t in session.execute(
                select(Transaction)
                .where(Transaction.id.in_(ids))
                .options(
                    selectinload(Transaction.postings).selectinload(Posting.account),
                    selectinload(Transaction.payee),
                )
            ).scalars()
        }
        if ids
        else {}
    )
    notable: list[tuple[Decimal, dict[str, Any]]] = []
    for event in event_rows:
        tx = txs.get(event.entity_id) if event.entity_id else None
        leg = _real_leg(tx) if tx is not None else None
        if tx is None or leg is None:
            continue
        amount = Decimal(leg.amount)
        if abs(amount) > materiality:
            notable.append(
                (
                    abs(amount),
                    {
                        "payee": tx.payee.name if tx.payee else None,
                        # A payee-less row is labelled by its description, in its
                        # own key so minimal disclosure can withhold it (#437).
                        **({"description": tx.description} if not tx.payee else {}),
                        "amount": str(amount),
                        "transaction_id": str(tx.id),
                    },
                )
            )
    notable.sort(key=lambda entry: entry[0], reverse=True)
    return {
        "count": len(event_rows),
        "notable": [line for _, line in notable[:PENDINGS_VANISHED_CAP]],
        "truncated": len(notable) > PENDINGS_VANISHED_CAP,
    }


def _match_lines(
    session: Session, confirmed: list[Event], breached_match_ids: set[uuid.UUID]
) -> tuple[list[dict[str, Any]], bool]:
    ids = [e.entity_id for e in confirmed if e.entity_id is not None]
    matches = (
        {m.id: m for m in session.execute(select(Match).where(Match.id.in_(ids))).scalars()}
        if ids
        else {}
    )
    posted_ids = [m.posted_id for m in matches.values() if m.posted_id is not None]
    posted_txns: dict[uuid.UUID, Transaction] = (
        {
            t.id: t
            for t in session.execute(
                select(Transaction)
                .where(Transaction.id.in_(posted_ids))
                .options(
                    selectinload(Transaction.postings).selectinload(Posting.account),
                    selectinload(Transaction.payee),
                )
            ).scalars()
        }
        if posted_ids
        else {}
    )
    lines: list[dict[str, Any]] = []
    for event in confirmed:
        match = matches.get(event.entity_id) if event.entity_id else None
        if match is None:
            continue
        posted = posted_txns.get(match.posted_id)
        name = None
        amount = Decimal(0)
        if posted is not None:
            leg = _real_leg(posted)
            amount = Decimal(leg.amount) if leg is not None else Decimal(0)
            name = posted.payee.name if posted.payee else posted.description
        lines.append(
            {
                "name": name or "matched bill",
                "amount": str(amount),
                "variance": str(Decimal(match.variance or 0)),
                "match_id": str(match.id),
                "_breach": match.id in breached_match_ids,
            }
        )
    # variance breaches are always itemized: they sort ahead of the cap
    lines.sort(key=lambda entry: not entry["_breach"])
    truncated = len(lines) > MATCHES_CAP
    lines = lines[:MATCHES_CAP]
    for entry in lines:
        entry.pop("_breach")
    return lines, truncated


def _bill_leg(tx: Transaction, schedules: dict[uuid.UUID, Schedule]) -> Posting | None:
    """The leg an upcoming row is a bill from (#357). A flat row has one real
    leg. A transfer schedule's instance (card or loan payment, contribution)
    has two, and the bill is the leg on the schedule's own account — the
    paying side, the perspective leg the matcher and the forecast use; a
    transfer INTO that account is an inflow there, so never a bill. Any other
    two-leg expected row names no paying side and is not listed."""
    schedule = schedules.get(tx.schedule_id) if tx.schedule_id is not None else None
    if schedule is not None and schedule.counterparty_account_id is not None:
        return next((p for p in tx.postings if p.account_id == schedule.account_id), None)
    return _real_leg(tx)


def _upcoming(session: Session, today: Any) -> dict[str, Any]:
    """The week's bills: expected outflows due in the next seven days, read
    from the paying leg (``_bill_leg``). An instance an open proposal already
    settles against a live bank row is paid, pending confirmation (decision
    76) — the same rule the forecast applies — so it is not listed as still
    to come."""
    from purser import matching

    rows = list(
        session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.status == "expected",
                Transaction.superseded_by.is_(None),
                Transaction.date >= today,
                Transaction.date <= today + timedelta(days=7),
            )
            .order_by(Transaction.date)
        ).scalars()
    )
    settled = matching.proposed_settlements(
        session, account_id=None, expected_ids=[tx.id for tx in rows]
    )
    schedule_ids = {tx.schedule_id for tx in rows if tx.schedule_id is not None}
    schedules = (
        {
            s.id: s
            for s in session.execute(
                select(Schedule).where(Schedule.id.in_(schedule_ids))
            ).scalars()
        }
        if schedule_ids
        else {}
    )
    total = Decimal(0)
    items: list[dict[str, Any]] = []
    for tx in rows:
        if tx.id in settled:
            continue
        leg = _bill_leg(tx, schedules)
        amount = Decimal(leg.amount) if leg is not None else Decimal(0)
        if amount >= 0:
            continue  # the week's bills, not expected income
        total += -amount
        items.append(
            {
                "date": tx.date.isoformat(),
                "name": tx.payee.name if tx.payee else (tx.description or "expected"),
                "amount": str(amount),
                "transaction_id": str(tx.id),
            }
        )
    return {
        "count": len(items),
        "total": str(total.quantize(Decimal("0.01"))),
        "items": items[:UPCOMING_CAP],
        "truncated": len(items) > UPCOMING_CAP,
    }


SUGGESTIONS_CAP = 3


def _suggestions_block(session: Session) -> dict[str, Any]:
    """Open inbox count plus the top few by confidence (#233): "what needs my
    attention" in one section, never the whole inbox."""
    from purser import suggestions

    rows = suggestions.list_suggestions(session, limit=SUGGESTIONS_CAP)
    return {
        "open": suggestions.open_count(session),
        "top": [
            {
                "id": str(r.id),
                "headline": suggestions.headline(r),
                "confidence": str(r.confidence) if r.confidence is not None else None,
                "source": r.source,
            }
            for r in rows
        ],
    }


def _review_block(session: Session) -> dict[str, Any]:
    """Unreviewed count + how stale the oldest is (#175). `oldest_days` is
    the leading indicator; the aged sweep is the backstop."""
    from purser import review

    return {
        "unreviewed": review.unreviewed_count(session),
        "oldest_days": review.oldest_unreviewed_days(session),
    }


def whats_new(
    session: Session,
    *,
    since_cursor: str | None = None,
    materiality: Decimal = DEFAULT_MATERIALITY,
    scan_limit: int = SCAN_LIMIT,
) -> dict[str, Any]:
    """Everything material since the cursor, pre-summarized per section.

    Cursor semantics: pass the returned ``cursor`` next time to get only new
    events (keyset over (at, id) — same recipe as webhook replay, issue #52).
    A full ``scan_limit`` page sets ``events_truncated``; calling again with
    the returned cursor resumes exactly where this call stopped.
    """
    since_id = uuid.UUID(since_cursor) if since_cursor else None
    now = datetime.now(UTC)
    events = webhooks.replay_events(
        session,
        since_id=since_id,
        since=None if since_id else now - timedelta(days=DEFAULT_WINDOW_DAYS),
        limit=scan_limit,
    )
    by_type: dict[str, list[Event]] = {}
    for event in events:
        by_type.setdefault(event.type, []).append(event)

    breached_match_ids = {
        e.entity_id for e in by_type.get("variance.threshold", []) if e.entity_id is not None
    }
    matches, matches_truncated = _match_lines(
        session, by_type.get("match.confirmed", []), breached_match_ids
    )

    # The schedule's payee as it is named now, not as the event recorded it,
    # so the minimal-disclosure scrub (#437) recognizes it after a rename.
    def _schedule_uuid(raw: Any) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(raw)) if raw else None
        except ValueError:
            return None

    schedule_ids = {
        sid
        for e in by_type.get("recurring.price_changed", [])
        if (sid := _schedule_uuid(e.payload.get("schedule_id"))) is not None
    }
    current_payee: dict[uuid.UUID, str] = (
        {
            schedule_id: name
            for schedule_id, name in session.execute(
                select(Schedule.id, Payee.name)
                .join(Payee, Payee.id == Schedule.payee_id)
                .where(Schedule.id.in_(schedule_ids))
            )
        }
        if schedule_ids
        else {}
    )
    price_changes = [
        {
            "payee": current_payee.get(
                _schedule_uuid(e.payload.get("schedule_id")) or uuid.UUID(int=0),
                e.payload.get("payee", "?"),
            ),
            "delta": e.payload.get("delta", "0"),
            "schedule_id": e.payload.get("schedule_id", ""),
        }
        for e in by_type.get("recurring.price_changed", [])
    ]
    forecast_flags = [
        {
            "account": e.payload.get("account") or str(e.entity_id),
            "low": e.payload.get("low"),
            "date": e.payload.get("date", ""),
        }
        for e in by_type.get("forecast.shortfall", [])
    ]
    balance_drifts = [_drift_line(e) for e in by_type.get("balance.drift", [])]
    # A manual position's symbol turned stale or has no price (#390): one line
    # per transition, newest last, like the stream they come from.
    stale_prices = [
        {
            "symbol": e.payload.get("symbol", "?"),
            "price_as_of": e.payload.get("price_as_of"),
        }
        for e in by_type.get("price.stale", [])
    ]
    # A feed position with no value that no stored close prices (#392): one
    # line per account and symbol when it turns unpriced; unpriceable when
    # its symbol is no ticker (no recorded close can value it).
    unpriced_holdings = [
        {
            "account": e.payload.get("account", "?"),
            "symbol": e.payload.get("symbol", "?"),
            "quantity": e.payload.get("quantity"),
            "as_of": e.payload.get("as_of"),
            **({"unpriceable": True} if e.payload.get("unpriceable") else {}),
        }
        for e in by_type.get("holding.unpriced", [])
    ]

    # A feed account still showing its hand-tracked set because no fetch has
    # ended its manual period (#422): one line per account and switch.
    open_periods = [
        {
            "account": e.payload.get("account", "?"),
            "since": e.payload.get("since"),
        }
        for e in by_type.get("holding.period_open", [])
    ]

    return {
        "cursor": str(events[-1].id) if events else since_cursor,
        "transactions": _tx_lines(session, by_type.get("transaction.posted", []), materiality),
        "matches": matches,
        "matches_truncated": matches_truncated,
        "price_changes": price_changes[:PRICE_CHANGES_CAP],
        "price_changes_truncated": len(price_changes) > PRICE_CHANGES_CAP,
        "forecast_flags": forecast_flags[:FORECAST_FLAGS_CAP],
        "balance_drifts": balance_drifts[:BALANCE_DRIFTS_CAP],
        "balance_drifts_truncated": len(balance_drifts) > BALANCE_DRIFTS_CAP,
        "pendings_vanished": _vanished_lines(
            session, by_type.get("pending.vanished", []), materiality
        ),
        "stale_prices": stale_prices[:STALE_PRICES_CAP],
        "stale_prices_truncated": len(stale_prices) > STALE_PRICES_CAP,
        "unpriced_holdings": unpriced_holdings[:UNPRICED_HOLDINGS_CAP],
        "unpriced_holdings_truncated": len(unpriced_holdings) > UNPRICED_HOLDINGS_CAP,
        "open_manual_periods": open_periods[:OPEN_PERIODS_CAP],
        "open_manual_periods_truncated": len(open_periods) > OPEN_PERIODS_CAP,
        "upcoming": _upcoming(session, now.date()),
        "review": _review_block(session),
        "suggestions": _suggestions_block(session),
        "sync": {"failures": len(by_type.get("sync.failed", []))},
        "events_truncated": len(events) == scan_limit,
    }
