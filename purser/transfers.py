"""Deterministic transfer auto-linking (DESIGN §5).

Two phases, run after every import and in the daily matching sweep:

1. **Absorb** — a newly imported one-sided row that matches an existing
   transfer's *unbacked* leg (same account, same amount, inside the date
   window, and carrying a transfer-style name signal) is voided into that
   transfer. This is how the far side of a manually converted transfer is
   claimed when the bank finally reports it, instead of duplicating.
2. **Pair-link** — opposite-amount pairs on different accounts inside the
   window that carry a signal — their text names the transfer (keyword
   token or counterparty account name), an active ``transfer_link`` rule
   declares the pair, or a transfer schedule's instance covers it (decision
   65) — are merged automatically with a system actor. Pairs matching on
   amount+date alone stay confirm-first candidates. A bank leg the matcher
   already holds for an expected transfer still pairs with that transfer's
   other side, and the settlement follows it into the merge.

An active ``transfer_link`` rule stands in for the name signal on the account
pair it declares, and may give that pair its own date window (decision 66).
``plan_auto_links`` is the selection; ``auto_link_pass`` executes it and the
rule activation confirm counts it.

Never unlogged: every merge/absorb writes an audit row and emits an event,
the register shows the link, and unlink resurrects the originals. Pairs a
human explicitly unlinked are suppressed from the AUTO paths only — an
explicit confirm or convert can always relink them. Kill switch:
``transfer_auto_link`` (Config).
"""

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session, selectinload

from purser import appsettings, audit, balances, events, matching
from purser import ledger as ledger_service
from purser import review as review_service
from purser import suggestions as suggestions_service
from purser.admin import security
from purser.core import ledger as core
from purser.core import matching as core_matching
from purser.ledger import LedgerError
from purser.logging import get_logger
from purser.models import ApiToken, Event, Match, Posting, Schedule, Transaction

if TYPE_CHECKING:
    from purser.rules import DeclaredPair

log = get_logger(__name__)

# The standard window. A transfer_link rule may set its own for the pair it
# declares (decision 66); everything else — including explicit conversions —
# pairs within this one.
_WINDOW = timedelta(days=core_matching.TRANSFER_WINDOW_DAYS)


def _signal_text(tx: Transaction) -> str:
    payee = tx.payee.name if tx.payee else ""
    return f"{payee} {tx.description or ''}"


def _unlinked_pair_keys(session: Session) -> set[str]:
    # pair_key moved to the plaintext dedup_key column (#137): the payload is
    # now encrypted, so a JSONB projection can't read it.
    rows = session.execute(
        select(Event.dedup_key).where(Event.type == "transfer.unlinked")
    ).scalars()
    return {key for key in rows if key}


def _unlinked_absorbs(session: Session) -> set[tuple[str, str]]:
    """(transfer_id, content_hash) pairs a human explicitly unlinked."""
    suppressed: set[tuple[str, str]] = set()
    rows = session.execute(select(Event.payload).where(Event.type == "transfer.unlinked")).scalars()
    for payload in rows:
        transfer_id = payload.get("transfer_id")
        for resurrected in payload.get("resurrected", []):
            content_hash = resurrected.get("content_hash")
            if transfer_id and content_hash:
                suppressed.add((transfer_id, content_hash))
    return suppressed


@dataclass
class _OpenLeg:
    """An unbacked side of a live transfer — absorb phase target."""

    transfer: Transaction
    account_id: uuid.UUID
    amount: Decimal
    counterparty_account_id: uuid.UUID
    counterparty_name: str


def _open_transfer_legs(session: Session) -> list[_OpenLeg]:
    """Legs of live chain-head transfers that no voided import backs yet.

    Loads live chain-head transactions and filters to the PLAIN transfer
    shape (two real legs that cancel, no virtual) in Python — transfers are a
    small slice of the ledger, and this keeps it to two queries for the whole
    phase. A split loan payment is never an absorb target: its unequal legs
    were both claimed from bank rows when it was confirmed.
    """
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    rows = list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(
                Transaction.status.in_(matching.MATCHABLE_STATUSES),
                Transaction.superseded_by.is_(None),
                Transaction.id.not_in(reversed_ids),
            )
        ).scalars()
    )
    transfers = [t for t in rows if is_transfer_shaped(t)]
    if not transfers:
        return []
    backed: set[tuple[uuid.UUID, uuid.UUID]] = set()  # (transfer_id, account_id)
    children = session.execute(
        select(Transaction)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
        .where(
            Transaction.superseded_by.in_([t.id for t in transfers]),
            Transaction.status == "void",
            # Backing means "this bank line is accounted for" — so only
            # children still HOLDING a content hash count. Unlink moves the
            # hash onto the resurrected clone (leg opens again), and voided
            # manual originals never had one (the bank's own rows for that
            # transfer should still be absorbable).
            Transaction.content_hash.is_not(None),
        )
    ).scalars()
    for child in children:
        for p in child.postings:
            if not p.account.is_virtual and child.superseded_by is not None:
                backed.add((child.superseded_by, p.account_id))
    legs: list[_OpenLeg] = []
    for t in transfers:
        for leg in t.postings:
            if (t.id, leg.account_id) in backed:
                continue
            if leg.account.closed_at is not None:
                continue  # a closed account leaves the absorb pool (DESIGN §3.4)
            other = next(p for p in t.postings if p is not leg)
            legs.append(
                _OpenLeg(
                    transfer=t,
                    account_id=leg.account_id,
                    amount=Decimal(leg.amount),
                    counterparty_account_id=other.account_id,
                    counterparty_name=other.account.name,
                )
            )
    return legs


@dataclass(frozen=True)
class _Instance:
    """An instance of an active transfer schedule, reduced to what it says
    about a bank pair (decision 65): from where, to where, how much, when."""

    expected_id: uuid.UUID
    schedule_id: uuid.UUID
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount: Decimal  # the transfer amount, positive
    date: date_type


def _transfer_schedule_ids(session: Session) -> set[uuid.UUID]:
    """Active schedules with a counterparty: the only ones that vouch."""
    return set(
        session.execute(
            select(Schedule.id).where(
                Schedule.status == "active", Schedule.counterparty_account_id.is_not(None)
            )
        ).scalars()
    )


def _as_instance(tx: Transaction, active: set[uuid.UUID]) -> _Instance | None:
    if tx.schedule_id is None or tx.schedule_id not in active:
        return None
    if transfer_shape(tx) is not core.TransferShape.PLAIN:
        return None
    outgoing, incoming = sorted(tx.postings, key=lambda p: Decimal(p.amount))
    return _Instance(
        expected_id=tx.id,
        schedule_id=tx.schedule_id,
        from_account_id=outgoing.account_id,
        to_account_id=incoming.account_id,
        amount=Decimal(incoming.amount),
        date=tx.date,
    )


def _open_instances(session: Session, active: set[uuid.UUID]) -> list[_Instance]:
    """Unsettled instances of active transfer schedules, oldest first. A
    live *proposal* leaves an instance open; a confirmed match settles it."""
    if not active:
        return []
    rows = session.execute(
        select(Transaction)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
        .where(
            Transaction.status == "expected",
            Transaction.superseded_by.is_(None),
            Transaction.schedule_id.in_(active),
        )
        .order_by(Transaction.date, Transaction.created_at, Transaction.id)
    ).scalars()
    return [instance for tx in rows if (instance := _as_instance(tx, active)) is not None]


def _held_instances(
    holds: dict[uuid.UUID, matching.TransferHold],
    rows: tuple[Transaction, Transaction],
    active: set[uuid.UUID],
) -> list[_Instance]:
    """The instances the pair's own legs are held for (proposed or settled)."""
    out: list[_Instance] = []
    for row in rows:
        hold = holds.get(row.id)
        instance = _as_instance(hold.expected, active) if hold is not None else None
        if instance is not None:
            out.append(instance)
    return out


def _vouching_instance(
    instances: list[_Instance],
    *,
    out_leg: Posting,
    in_leg: Posting,
    out_date: date_type,
    in_date: date_type,
) -> _Instance | None:
    """The first instance that covers this pair, or None: the same two
    accounts in the same direction, exactly the instance's amount, and both
    legs inside the transfer window of the instance's date.

    Like a transfer_link rule it replaces only the name-signal test (the
    pool, posted-only and unlink suppression still apply); unlike one it
    needs no activation confirm, because it cannot sweep history: an
    instance vouches only while it is open — materialized ahead, or overdue
    — or for a pair holding the very row that settles it, and the merge it
    vouches for settles it, so it vouches once."""
    amount = -Decimal(out_leg.amount)
    for instance in instances:
        if (
            instance.from_account_id == out_leg.account_id
            and instance.to_account_id == in_leg.account_id
            and instance.amount == amount
            and abs(instance.date - out_date) <= _WINDOW
            and abs(instance.date - in_date) <= _WINDOW
        ):
            return instance
    return None


def _absorb(session: Session, *, row: Transaction, leg: _OpenLeg, signal: str) -> None:
    _absorb_row(
        session,
        row=row,
        transfer=leg.transfer,
        account_id=leg.account_id,
        amount=leg.amount,
        signal=signal,
        actor_token_id=None,
    )


def _absorb_row(
    session: Session,
    *,
    row: Transaction,
    transfer: Transaction,
    account_id: uuid.UUID,
    amount: Decimal,
    signal: str,
    actor_token_id: uuid.UUID | None,
) -> None:
    """Void the one-sided bank ``row`` into ``transfer`` — the auto-link
    pass's absorb and an explicit conversion's share this. A posted row
    posts a pending transfer: the bank posting one side is authoritative for
    the whole transfer.

    That flip, and the row's move under the transfer, can move a schedule
    estimate (a confirmed match's lineage head turning posted), so the change
    is bracketed with the probe ``transition_status`` uses (#330, #343)."""
    probes = matching.estimate_probes_on(session, transfer, row)
    prior_status = row.status
    if row.status == "posted" and transfer.status == "pending":
        transfer.status = "posted"
    row.status = "void"
    row.superseded_by = transfer.id
    session.flush()
    # Absorb removes a countable row; the transfer's pending->posted flip is
    # membership-neutral (decision 38).
    balances.mark_dirty(session, row.date)
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="transfer.absorb",
        entity_type="transaction",
        entity_id=transfer.id,
        after={
            "absorbed_id": str(row.id),
            "account_id": str(account_id),
            "amount": str(amount),
            "signal": signal,
        },
    )
    events.emit(
        session,
        type_="transfer.absorbed",
        entity_type="transaction",
        entity_id=transfer.id,
        payload={
            "transfer_id": str(transfer.id),
            "absorbed_id": str(row.id),
            "absorbed_status": prior_status,
            "content_hash": row.content_hash,
            "signal": signal,
        },
    )
    matching.emit_estimate_changes(session, probes)


@dataclass(frozen=True)
class PlannedAbsorb:
    """A posted bank row the pass would void into an existing transfer."""

    row: Transaction
    leg: _OpenLeg
    rule_id: uuid.UUID | None  # the declaring transfer_link rule; None = name signal
    gap_days: int


@dataclass(frozen=True)
class PlannedMerge:
    """Two one-sided posted rows the pass would merge into one transfer."""

    outgoing: Transaction
    incoming: Transaction
    rule_id: uuid.UUID | None  # the declaring transfer_link rule; None = name/schedule
    gap_days: int
    # Why it links, recorded on the event: ``rule:<id>``, ``name`` or
    # ``schedule:<id>`` (decision 65), first applicable in that order.
    signal: str
    # The open transfer-schedule instance this merge covers: settled by it as
    # soon as it is made (decision 65).
    settles: uuid.UUID | None = None


@dataclass(frozen=True)
class LinkPlan:
    absorbs: list[PlannedAbsorb]
    merges: list[PlannedMerge]


def plan_auto_links(session: Session, declared: "list[DeclaredPair]") -> LinkPlan:
    """Every absorb and merge the auto-link pass would make, without making
    any. ``declared`` is ``rules.declared_pairs(...)`` — the active set, or for
    a projection the active set plus the rule being previewed.

    THE one place the pass's selection lives: ``auto_link_pass`` executes the
    plan, and the transfer_link activation confirm counts it
    (``rules.project_links``) — so the number a user confirms is the number
    the next pass links (decision 66), absorbs included.

    Two stages, the first finished before the second starts, so a rule's
    widened window can never change what the standard window decides:

    1. **Standard** — absorb, then pair, over ±TRANSFER_WINDOW_DAYS exactly as
       the pass ran before rules had windows: a link needs a name signal, or
       a transfer_link rule that declares the account pair *and* whose own
       window covers the gap (so a rule may narrow its pair below ±3), or —
       pairs only — a transfer schedule's instance that covers the pair
       (decision 65).
    2. **Widened** — only when some rule's ``window_days`` exceeds the global
       window: absorb, then pair, that rule's declared accounts farther apart,
       over the rows stage 1 left alone — not absorbed, and not in any pair it
       found, linked or left as a confirm-first candidate — and into the open
       legs it left unclaimed.

    Both pair phases draw on ``matching.transfer_pool``: a bank leg the
    matcher holds for an expected transfer pairs too, only with that
    expected's other side, and the merge carries its match (decision 65).
    Absorbs never take a held leg. The pool is sorted (date, created_at, id)
    so equal-date ties resolve the same way in the projection and in the
    pass.
    """
    from purser import rules as rules_service

    # AUTO paths act on POSTED rows only. A bank-pending row can come back
    # posted under a different description (new content hash — the known
    # pending->posted rehash edge); consuming it in a merge first would turn
    # that rehash into a duplicated leg. Pending pairs stay confirm-first
    # candidates until they post.
    #
    # The transfer pool (decision 65): unmatched one-sided rows, plus the bank
    # legs the matcher holds for an expected transfer. A held leg pairs only
    # with that expected's other side, and is never absorbed — the absorb
    # paths do not carry matches.
    rows, holds = matching.transfer_pool(session)
    pool = sorted(
        (r for r in rows if r.status == "posted"),
        key=lambda t: (t.date, t.created_at, t.id),
    )
    # Open legs are taken from this one map by both stages, so a leg claimed
    # by the standard stage is gone before the widened one looks.
    open_legs: dict[tuple[uuid.UUID, Decimal], list[_OpenLeg]] = {}
    for leg in _open_transfer_legs(session):
        open_legs.setdefault((leg.account_id, leg.amount), []).append(leg)
    suppressed_absorbs = _unlinked_absorbs(session) if open_legs else set()
    suppressed_pairs = _unlinked_pair_keys(session)
    # Open instances of active transfer schedules, taken from this one list by
    # both stages: each covers one merge at most.
    active = _transfer_schedule_ids(session)
    instances = _open_instances(session, active)

    def rest(taken: set[uuid.UUID]) -> list[Transaction]:
        return [r for r in pool if r.id not in taken]

    def unheld(rows: list[Transaction]) -> list[Transaction]:
        return [r for r in rows if r.id not in holds]

    absorbs = _plan_absorbs(unheld(pool), open_legs, declared, suppressed_absorbs, widened=False)
    taken = {a.row.id for a in absorbs}
    merges, paired = _plan_merges(
        session,
        rest(taken),
        declared,
        suppressed_pairs,
        holds=holds,
        instances=instances,
        active=active,
    )
    taken |= paired

    widened = [p for p in declared if p.window_days > core_matching.TRANSFER_WINDOW_DAYS]
    if widened:
        reachable = {a for p in widened for a in p.accounts}
        far_absorbs = _plan_absorbs(
            unheld(rest(taken)), open_legs, declared, suppressed_absorbs, widened=True
        )
        taken |= {a.row.id for a in far_absorbs}
        far_rows = [
            r
            for r in rest(taken)
            if (real := matching._real_leg(r)) is not None and real.account_id in reachable
        ]
        payee_of = {r.id: r.payee_id for r in far_rows}

        def window(out: core_matching.MatchInput, inc: core_matching.MatchInput) -> int | None:
            assert out.account_id is not None and inc.account_id is not None
            days = rules_service.declared_window(
                widened,
                out.account_id,
                inc.account_id,
                out.amount,
                out.date,
                payee_ids=(payee_of.get(out.id), payee_of.get(inc.id)),
            )
            return days if days is not None and days > core_matching.TRANSFER_WINDOW_DAYS else None

        far_merges, _ = _plan_merges(
            session,
            far_rows,
            declared,
            suppressed_pairs,
            holds=holds,
            instances=instances,
            active=active,
            window=window,
        )
        absorbs += far_absorbs
        merges += far_merges
    return LinkPlan(absorbs=absorbs, merges=merges)


def _plan_absorbs(
    pool: list[Transaction],
    open_legs: dict[tuple[uuid.UUID, Decimal], list[_OpenLeg]],
    declared: "list[DeclaredPair]",
    suppressed: set[tuple[str, str]],
    *,
    widened: bool,
) -> list[PlannedAbsorb]:
    """Rows claimed by existing transfers with an unbacked side. Standard:
    within ±3 days, on a name signal or a declaration whose window covers the
    gap. Widened: farther apart, on a declaration only. Claimed legs leave
    ``open_legs``."""
    from purser import rules as rules_service

    planned: list[PlannedAbsorb] = []
    if not open_legs:
        return planned
    for row in pool:
        real = matching._real_leg(row)
        assert real is not None  # _one_sided_import_rows guarantees it
        amount = Decimal(real.amount)
        candidates: list[tuple[int, _OpenLeg, uuid.UUID | None]] = []
        for leg in open_legs.get((real.account_id, amount), []):
            if leg.transfer.id == row.id:
                continue
            if (str(leg.transfer.id), row.content_hash) in suppressed:
                continue
            gap = abs((row.date - leg.transfer.date).days)
            rule_id = rules_service.pair_declared(
                declared,
                real.account_id,
                leg.counterparty_account_id,
                amount,
                row.date,
                gap_days=gap,
                payee_ids=(row.payee_id, leg.transfer.payee_id),
            )
            if widened:
                eligible = gap > core_matching.TRANSFER_WINDOW_DAYS and rule_id is not None
            else:
                eligible = gap <= core_matching.TRANSFER_WINDOW_DAYS and (
                    rule_id is not None
                    or core_matching.has_transfer_signal(
                        text=_signal_text(row), counterparty_name=leg.counterparty_name
                    )
                )
            if eligible:
                candidates.append((gap, leg, rule_id))
        if not candidates:
            continue
        gap, best, rule_id = min(
            candidates,
            key=lambda c: (c[0], c[1].transfer.created_at, c[1].transfer.id),
        )
        planned.append(PlannedAbsorb(row=row, leg=best, rule_id=rule_id, gap_days=gap))
        open_legs[(real.account_id, amount)].remove(best)
    return planned


def _plan_merges(
    session: Session,
    pool: list[Transaction],
    declared: "list[DeclaredPair]",
    suppressed: set[str],
    *,
    holds: dict[uuid.UUID, matching.TransferHold],
    instances: list[_Instance],
    active: set[uuid.UUID],
    window: core_matching.PairWindow | None = None,
) -> tuple[list[PlannedMerge], set[uuid.UUID]]:
    """Opposite pairs to merge into one transfer, plus the id of every row a
    detected pair used — linked or not. ``window=None`` is the standard stage
    (a covering declaration, a name signal, or a transfer schedule's instance
    covering the pair links it — decision 65); a window function is the
    widened stage, which admits declared pairs only. A row in ``holds`` pairs
    only with its expected's other side. The instance a planned merge covers
    is recorded for settlement and leaves ``instances``, so it covers one."""
    from purser import rules as rules_service

    pairs, tx_of = matching.detect_transfer_pairs_for(session, pool, holds=holds, window=window)
    paired: set[uuid.UUID] = set()
    planned: list[PlannedMerge] = []
    for pair in pairs:
        paired.update((pair.outgoing_id, pair.incoming_id))
        if matching._pair_key(pair.outgoing_id, pair.incoming_id) in suppressed:
            continue
        outgoing, incoming = tx_of[pair.outgoing_id], tx_of[pair.incoming_id]
        out_leg, in_leg = matching._real_leg(outgoing), matching._real_leg(incoming)
        assert out_leg is not None and in_leg is not None  # the pool guarantees it
        rule_id = rules_service.pair_declared(
            declared,
            out_leg.account_id,
            in_leg.account_id,
            Decimal(out_leg.amount),
            outgoing.date,
            gap_days=pair.date_gap_days,
            payee_ids=(outgoing.payee_id, incoming.payee_id),
        )
        # The instance a leg's match holds speaks first; then any open
        # instance of an active transfer schedule.
        covering = _vouching_instance(
            _held_instances(holds, (outgoing, incoming), active) + instances,
            out_leg=out_leg,
            in_leg=in_leg,
            out_date=outgoing.date,
            in_date=incoming.date,
        )
        if rule_id is not None:
            signal = f"rule:{rule_id}"
        elif window is not None:
            continue  # the widened stage links declared pairs only
        elif core_matching.has_transfer_signal(
            text=_signal_text(outgoing), counterparty_name=in_leg.account.name
        ) or core_matching.has_transfer_signal(
            text=_signal_text(incoming), counterparty_name=out_leg.account.name
        ):
            signal = "name"
        elif covering is not None:
            signal = f"schedule:{covering.schedule_id}"
        else:
            continue  # stays a confirm-first candidate
        if covering is not None:
            instances[:] = [i for i in instances if i.expected_id != covering.expected_id]
        planned.append(
            PlannedMerge(
                outgoing=outgoing,
                incoming=incoming,
                rule_id=rule_id,
                gap_days=pair.date_gap_days,
                signal=signal,
                settles=covering.expected_id if covering is not None else None,
            )
        )
    return planned, paired


def auto_link_pass(session: Session) -> dict[str, int]:
    """Absorb + pair-link over the current one-sided import pool.

    No-op when ``transfer_auto_link`` is off. The caller owns the commit.
    Selection is ``plan_auto_links``; this executes it.
    """
    stats = {"transfers_absorbed": 0, "transfers_auto_linked": 0}
    if not appsettings.effective().transfer_auto_link:
        return stats

    # Declared transfer pairs (#174, decision 46; windows, decision 66). A
    # transfer_link rule replaces the name-signal test for its pair and sets
    # that pair's date window — the amount, posted-only, cross-account and
    # unlink-suppression conditions all still apply, so a declared pair is a
    # narrower claim than it looks. Loaded once per pass, like the rest of
    # the active set.
    from purser import rules as rules_service

    declared = rules_service.declared_pairs(rules_service.load_active(session))
    plan = plan_auto_links(session, declared)

    # A link a declaration made is one application of that rule (#314), the
    # way a categorize or rename rule counts each row it shapes: the rule the
    # link's signal names, once per link, in both stages and both phases.
    # Counted here, where links are made, so the activation confirm's
    # projection (the same plan) applies nothing; unlink takes nothing back.
    for absorb in plan.absorbs:
        signal = "name" if absorb.rule_id is None else f"rule:{absorb.rule_id}"
        _absorb(session, row=absorb.row, leg=absorb.leg, signal=signal)
        stats["transfers_absorbed"] += 1
        if absorb.rule_id is not None:
            rules_service.mark_applied(session, [absorb.rule_id])

    for merge in plan.merges:
        # The merge carries any match on its rows (decision 65); a covered
        # instance still open is then settled by the merge at once, so the
        # forecast never counts it beside the posted legs.
        matching._merge_transfer_pair(
            session,
            outgoing=merge.outgoing,
            incoming=merge.incoming,
            token=None,
            origin=core.Origin.SYSTEM,
            action="transfer.auto_link",
            event_type="transfer.auto_linked",
            signal=merge.signal,
        )
        stats["transfers_auto_linked"] += 1
        if merge.rule_id is not None:
            rules_service.mark_applied(session, [merge.rule_id])
        if merge.settles is not None:
            instance = matching._load_tx(session, merge.settles)
            if instance.status == "expected":
                matching._match_expected(session, instance)

    if stats["transfers_absorbed"] or stats["transfers_auto_linked"]:
        log.info("transfer auto-link pass", **stats)
    return stats


# ---------------------------------------------------------------------------
# Bracket conversion + unlink (DESIGN §3.3/§4/§5)
# ---------------------------------------------------------------------------


def transfer_shape(tx: Transaction) -> core.TransferShape | None:
    """The transfer shape ``tx``'s postings form, or None (DESIGN §3.3,
    decision 62). The ONE classifier behind every "is this a transfer?"
    question — the flat view, the MCP rows, unlink, conversions, the account
    purge — so a multi-posting row that merely touches two real accounts
    (#245) is never shown or handled as a transfer."""
    if any(p.account is None for p in tx.postings):
        return None
    return core.transfer_shape(
        [core.Leg(p.account_id, Decimal(p.amount), p.account.is_virtual) for p in tx.postings]
    )


def split_lines(tx: Transaction) -> int:
    """How many category lines ``tx`` has when it is a split transaction —
    one real leg plus two or more category legs (DESIGN §3.3, decision 87) —
    and 0 for every other row. Split rows are read-only in v1: the categorize
    pass, a categorize rule's apply-history and the transfer pool leave them
    alone."""
    if any(p.account is None for p in tx.postings):
        return 0
    return core.split_line_count(
        [core.Leg(p.account_id, Decimal(p.amount), p.account.is_virtual) for p in tx.postings]
    )


def is_transfer_shaped(tx: Transaction) -> bool:
    """A PLAIN transfer: two real legs that cancel. The shape bracket
    conversion re-points and absorption claims; a split loan payment is
    neither (its legs are both bank-backed and deliberately unequal)."""
    return transfer_shape(tx) is core.TransferShape.PLAIN


def is_transfer(tx: Transaction) -> bool:
    """Any transfer Purser creates: PLAIN, or a SPLIT loan payment."""
    return transfer_shape(tx) is not None


def split_remainder(tx: Transaction) -> Posting | None:
    """The interest/fee posting of a split loan payment; None for any other
    row, including a multi-posting row that is not Purser's split shape."""
    if transfer_shape(tx) is not core.TransferShape.SPLIT:
        return None
    return next(p for p in tx.postings if p.account.is_virtual)


def linked_original_clause() -> ColumnElement[bool]:
    """SQL test that a void row superseded by another row is one of its
    linked ORIGINALS (a merged or absorbed row), not an expected it settles:
    a match supersedes its expected as well, but that is a settlement — unlink
    releases it instead of resurrecting it (decision 65). Correlated with the
    enclosing query's ``transactions``."""
    return ~(
        select(Match.id)
        .where(
            Match.expected_id == Transaction.id,
            Match.posted_id == Transaction.superseded_by,
            Match.state == "confirmed",
        )
        .correlate(Transaction)
        .exists()
    )


def has_linked_originals(session: Session, tx_id: uuid.UUID) -> bool:
    return (
        session.execute(
            select(Transaction.id)
            .where(
                Transaction.superseded_by == tx_id,
                Transaction.status == "void",
                linked_original_clause(),
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _perspective_leg(session: Session, tx: Transaction, account_ref: str | None) -> Posting:
    """The leg an amount edit of a PLAIN transfer is read from: the side
    ``account_ref`` names, else a schedule instance's schedule account, else
    none — an amount means nothing without a side (decision 56)."""
    legs = {p.account_id: p for p in tx.postings}
    if account_ref is not None:
        account = ledger_service.resolve_account(session, account_ref)
        if account.id not in legs:
            raise LedgerError(f"{account.path} is not a side of this transfer")
        return legs[account.id]
    if tx.schedule_id is not None:
        schedule = session.get(Schedule, tx.schedule_id)
        if schedule is not None and schedule.account_id in legs:
            return legs[schedule.account_id]
    sides = " or ".join(sorted(str(p.account.path) for p in tx.postings))
    raise LedgerError(f"name the side whose leg amount is (account={sides})")


def set_amount_in_place(
    session: Session, *, tx: Transaction, amount: Decimal, account_ref: str | None
) -> None:
    """The amount edit of an expected or pending transfer (DESIGN §3.3/§4,
    decision 78): ``amount`` is the leg of the side ``account_ref`` names (a
    schedule instance defaults to its schedule account), that leg takes it and
    the other leg its negation — zero-sum by construction, the direction
    fixed. Everything is validated before a posting changes; the caller
    (``ledger.update_transaction``) keeps pin, review mark, balance mark and
    the ``transaction.update`` audit.

    Refused: a split loan payment (its legs are its bank rows, deliberately
    unequal) and any transfer carrying linked bank rows — the bank amounts
    are the truth there, so the correction is an unlink."""
    if tx.status not in (core.Status.EXPECTED.value, core.Status.PENDING.value):
        raise LedgerError(f"a {tx.status} transfer is not edited in place")
    shape = transfer_shape(tx)
    if shape is core.TransferShape.SPLIT:
        raise LedgerError(
            "a split loan payment's amounts are its bank rows; unlink it and confirm again"
        )
    if shape is not core.TransferShape.PLAIN:
        raise LedgerError("only a transfer's two legs can be edited this way")
    if has_linked_originals(session, tx.id):
        raise LedgerError(
            "this transfer carries linked bank rows; unlink it first to change its amount"
        )
    leg = _perspective_leg(session, tx, account_ref)
    other = next(p for p in tx.postings if p is not leg)
    new_amount = core.quantize(amount)
    if new_amount == 0:
        raise LedgerError("amount must be nonzero")
    current = Decimal(leg.amount)
    if (new_amount < 0) != (current < 0):
        raise LedgerError(
            f"amount {new_amount} would reverse this transfer: its {leg.account.path} leg "
            f"is {current}; keep that sign, or name the other side in account"
        )
    leg.amount = new_amount
    other.amount = -new_amount


def posted_edit_refusal(session: Session, tx: Transaction) -> str:
    """Why a posted transfer cannot be edited, and what corrects it: a posted
    transfer is never re-booked in place (DESIGN §3.3/§4)."""
    if has_linked_originals(session, tx.id):
        return (
            "a posted transfer is corrected by unlinking it: its bank rows come back "
            "as separate transactions to correct and link again"
        )
    return (
        "a posted transfer cannot be edited; convert it back to a category "
        "(category plus account) and correct that row"
    )


def _require_commit(token: ApiToken | None, what: str) -> None:
    """``token=None`` is the system actor — the scheduled categorize pass and
    the rule engine run that way, and the caller gates scope upstream (the
    same convention as ``reverse_and_replace_with_specs`` and
    ``matching._merge_transfer_pair``)."""
    if token is not None and not security.can_write(token.scopes):
        raise LedgerError(f"{what} requires write scope")


def _corrected_once_guard(session: Session, tx: Transaction) -> None:
    correction = session.execute(
        select(Transaction.id).where(Transaction.reverses_id == tx.id)
    ).scalar_one_or_none()
    if correction is not None:
        raise LedgerError("transaction already corrected; edit the replacement instead")


def _audit_convert(
    session: Session, *, tx: Transaction, token: ApiToken | None, before: dict[str, Any]
) -> None:
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="transfer.convert",
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after=ledger_service._tx_audit_shape(tx, ledger_service._specs_of(tx)),
    )


def _absorb_target_for_convert(
    session: Session,
    *,
    row: Transaction,
    real: Posting,
    target_account_id: uuid.UUID,
    auto_pairing: bool = False,
) -> Transaction | None:
    """An existing live transfer connecting exactly (row's account <-> target)
    whose row-side leg is unbacked and matches amount + window. Explicit user
    intent: no name signal, no unlink suppression.

    ``auto_pairing`` is the machine path (DESIGN §11 rule 5): the model named
    the account, so the *pairing* must follow ``auto_link_pass``'s rules
    instead — posted rows only, and a pair a human unlinked stays unlinked.
    """
    amount = Decimal(real.amount)
    suppressed = _unlinked_absorbs(session) if auto_pairing else set()
    candidates = [
        leg
        for leg in _open_transfer_legs(session)
        if leg.account_id == real.account_id
        and leg.amount == amount
        and leg.counterparty_account_id == target_account_id
        and abs(row.date - leg.transfer.date) <= _WINDOW
        and leg.transfer.id != row.id
        and not (
            auto_pairing
            and (
                row.status != "posted"
                or leg.transfer.status != "posted"
                or (str(leg.transfer.id), row.content_hash) in suppressed
            )
        )
    ]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda leg: (
            abs((row.date - leg.transfer.date).days),
            leg.transfer.created_at,
            leg.transfer.id,
        ),
    )
    return best.transfer


def _counterpart_in(
    session: Session,
    *,
    account_id: uuid.UUID,
    amount: Decimal,
    row: Transaction,
    auto_pairing: bool = False,
) -> Transaction | None:
    """A live one-sided row in the target account with the opposite amount
    inside the window — the merge case of an explicit conversion.

    ``auto_pairing`` narrows the pool to what ``auto_link_pass`` would accept
    (DESIGN §11 rule 5): **posted rows only** — a bank-pending row can come
    back posted under a different description, and consuming it in a merge
    first turns that rehash into a duplicated leg — and pairs a human
    unlinked stay unlinked.

    A counterpart the matcher holds for an expected transfer to ``row``'s
    account is still a counterpart (decision 65, the auto-link pool's rule):
    without it, converting the late side made a second copy of the held
    leg. ``row`` must not hold a match itself — one merge carries one.
    """
    matched = select(Match.posted_id).where(Match.state.in_(["proposed", "confirmed"]))
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    rows = session.execute(
        select(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .options(
            selectinload(Transaction.postings).selectinload(Posting.account),
            selectinload(Transaction.payee),
        )
        .where(
            Transaction.status.in_(matching.MATCHABLE_STATUSES),
            Transaction.superseded_by.is_(None),
            Transaction.id.not_in(matched),
            Transaction.id.not_in(reversed_ids),
            Transaction.id != row.id,
            Posting.account_id == account_id,
            Posting.amount == amount,
            Transaction.date >= row.date - _WINDOW,
            Transaction.date <= row.date + _WINDOW,
        )
        .distinct()
    ).scalars()
    one_sided = [
        t
        for t in rows
        if matching._real_leg(t) is not None and matching._real_leg(t).account_id == account_id  # type: ignore[union-attr]
    ]
    row_leg = matching._real_leg(row)
    row_is_held = (
        session.execute(
            select(Match.id)
            .where(Match.posted_id == row.id, Match.state.in_(matching.LIVE_MATCH_STATES))
            .limit(1)
        ).first()
        is not None
    )
    if row_leg is not None and not row_is_held:
        held = [
            hold.row
            for hold in matching._transfer_holds(session, account_id=account_id).values()
            if hold.pair_account_id == row_leg.account_id
            and hold.row.id != row.id
            and abs(hold.row.date - row.date) <= _WINDOW
            and (leg := matching._real_leg(hold.row)) is not None
            and leg.account_id == account_id
            and Decimal(leg.amount) == amount
        ]
        if held:
            corrected = set(
                session.execute(
                    select(Transaction.reverses_id).where(
                        Transaction.reverses_id.in_([t.id for t in held])
                    )
                ).scalars()
            )
            one_sided += [t for t in held if t.id not in corrected]
    if auto_pairing:
        suppressed = _unlinked_pair_keys(session)
        one_sided = [
            t
            for t in one_sided
            if row.status == "posted"
            and t.status == "posted"
            and matching._pair_key(row.id, t.id) not in suppressed
        ]
    if not one_sided:
        return None
    return min(
        one_sided,
        key=lambda t: (abs((row.date - t.date).days), t.created_at, t.id),
    )


def convert_to_transfer(
    session: Session,
    *,
    tx: Transaction,
    target_ref: str,
    token: ApiToken | None,
    auto_pairing: bool = False,
) -> Transaction:
    """Category '[target]' semantics: make ``tx`` a transfer to ``target``.

    Resolution order: absorb into an existing transfer connecting the two
    accounts, else merge with a live one-sided counterpart in the target
    account, else convert in place (pending re-points the category leg;
    posted becomes a reversal + transfer-shaped replacement).

    ``token=None`` is the system actor (scope gated upstream).
    ``auto_pairing=True`` is the machine path — the AI classifier (DESIGN §11
    rule 5) and rule application. The model names the *account*; which rows
    pair stays deterministic, so absorb/merge candidates are narrowed to what
    ``auto_link_pass`` would accept: posted rows only, unlink-suppression
    respected. The conversion itself is unaffected.
    """
    target = ledger_service.resolve_account(session, target_ref)
    if target.is_virtual:
        raise LedgerError(f"{target.path} is a category — transfers connect two real accounts")

    if is_transfer(tx):
        return _repoint_transfer(session, tx=tx, target_account=target, token=token)

    real, category_leg = ledger_service._flat_legs(tx)
    if real.account_id == target.id:
        raise LedgerError("transfer accounts must differ")
    if real.currency.strip() != target.currency.strip():
        raise LedgerError("currency mismatch (v1 has no FX)")
    amount = Decimal(real.amount)
    signal = "rule" if auto_pairing else "user"

    if tx.status in matching.MATCHABLE_STATUSES:
        absorb_target = _absorb_target_for_convert(
            session,
            row=tx,
            real=real,
            target_account_id=target.id,
            auto_pairing=auto_pairing,
        )
        if absorb_target is not None:
            _require_commit(token, "absorbing into an existing transfer")
            _absorb_row(
                session,
                row=tx,
                transfer=absorb_target,
                account_id=real.account_id,
                amount=amount,
                signal=signal,
                actor_token_id=token.id if token else None,
            )
            return absorb_target

        counterpart = _counterpart_in(
            session,
            account_id=target.id,
            amount=-amount,
            row=tx,
            auto_pairing=auto_pairing,
        )
        if counterpart is not None:
            _require_commit(token, "merging a transfer pair")
            outgoing, incoming = (tx, counterpart) if amount < 0 else (counterpart, tx)
            return matching._merge_transfer_pair(
                session, outgoing=outgoing, incoming=incoming, token=token
            )

    if tx.status == core.Status.POSTED:
        _require_commit(token, "converting a posted transaction")
        _corrected_once_guard(session, tx)
        if amount < 0:
            specs = core.expand_transfer(
                from_account_id=real.account_id,
                to_account_id=target.id,
                amount=-amount,
                currency=real.currency.strip(),
            )
        else:
            specs = core.expand_transfer(
                from_account_id=target.id,
                to_account_id=real.account_id,
                amount=amount,
                currency=real.currency.strip(),
            )
        return ledger_service.reverse_and_replace_with_specs(
            session, tx=tx, token=token, replacement_specs=specs, action="transfer.convert"
        )

    before = ledger_service._tx_audit_shape(tx, ledger_service._specs_of(tx))
    # Assign the relationship (not just the FK): the session keeps objects
    # live across commit (expire_on_commit=False), so serializers reading
    # posting.account must see the new account immediately.
    category_leg.account = target
    session.flush()
    # The category leg just became a REAL leg: ``target`` gains this row.
    balances.mark_dirty(session, tx.date)
    _audit_convert(session, tx=tx, token=token, before=before)
    return tx


def _repoint_transfer(
    session: Session, *, tx: Transaction, target_account: Any, token: ApiToken | None
) -> Transaction:
    """'[C]' on an existing A<->B transfer: keep the perspective side, move
    the other. Linked originals must be unlinked first — re-pointing would
    orphan the far side's import backing."""
    if has_linked_originals(session, tx.id):
        raise LedgerError("unlink this transfer first — it carries linked bank rows")
    if target_account.id in {p.account_id for p in tx.postings}:
        raise LedgerError("that account is already a side of this transfer")
    raise LedgerError(
        "re-pointing a transfer is not supported yet: convert it back to a "
        "category first, then convert to the new transfer target"
    )


def convert_to_category(
    session: Session,
    *,
    tx: Transaction,
    category_ref: str,
    keep_account_ref: str | None,
    token: ApiToken,
) -> Transaction:
    """A plain category on a transfer-shaped row converts it back to a flat
    transaction. ``keep_account_ref`` names the side that keeps the row."""
    if has_linked_originals(session, tx.id):
        raise LedgerError("unlink this transfer first — it carries linked bank rows")
    if not is_transfer_shaped(tx):
        # Unreachable through the confirm path (a split always carries its
        # bank rows), kept so no caller can flatten one and drop a bank leg.
        raise LedgerError("a split loan payment cannot be recategorized; unlink it first")
    if keep_account_ref is None:
        raise LedgerError("recategorizing a transfer needs 'account': the side that keeps the row")
    kept_account = ledger_service.resolve_account(session, keep_account_ref)
    legs = {p.account_id: p for p in tx.postings}
    if kept_account.id not in legs:
        raise LedgerError(f"{kept_account.path} is not a side of this transfer")
    category = ledger_service.resolve_account(session, category_ref)
    if not category.is_virtual:
        raise LedgerError(f"{category.path} is not a category")
    kept = legs[kept_account.id]
    ledger_service.check_category_currency(kept.currency, category)
    other = next(p for p in tx.postings if p is not kept)

    if tx.status == core.Status.POSTED:
        _require_commit(token, "recategorizing a posted transfer")
        _corrected_once_guard(session, tx)
        specs = core.expand_flat(
            account_id=kept.account_id,
            category_id=category.id,
            amount=Decimal(kept.amount),
            currency=kept.currency.strip(),
        )
        return ledger_service.reverse_and_replace_with_specs(
            session, tx=tx, token=token, replacement_specs=specs, action="transfer.convert"
        )

    before = ledger_service._tx_audit_shape(tx, ledger_service._specs_of(tx))
    other.account = category  # relationship, not FK — see convert_to_transfer
    session.flush()
    # The re-pointed leg left a REAL account: that side loses this row.
    balances.mark_dirty(session, tx.date)
    _audit_convert(session, tx=tx, token=token, before=before)
    return tx


def _prior_statuses_for(session: Session, transfer_id: uuid.UUID) -> dict[str, str]:
    """child id -> its status before the merge/absorb, from event payloads."""
    prior: dict[str, str] = {}
    payloads = session.execute(
        select(Event.payload).where(
            Event.entity_id == transfer_id,
            Event.type.in_(["transfer.confirmed", "transfer.auto_linked", "transfer.absorbed"]),
        )
    ).scalars()
    for payload in payloads:
        if "absorbed_id" in payload:
            if payload.get("absorbed_status"):
                prior[payload["absorbed_id"]] = payload["absorbed_status"]
            continue
        for side in ("outgoing", "incoming"):
            row_id, status = payload.get(f"{side}_id"), payload.get(f"{side}_status")
            if row_id and status:
                prior[row_id] = status
    return prior


def _was_merge_created(session: Session, transfer_id: uuid.UUID) -> bool:
    return (
        session.execute(
            select(Event.id)
            .where(
                Event.entity_id == transfer_id,
                Event.type.in_(["transfer.confirmed", "transfer.auto_linked"]),
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _release_settlements_of(
    session: Session, tx: Transaction, *, actor_token_id: uuid.UUID | None
) -> None:
    """``tx`` was just voided: a void row settles nothing. A confirmed match
    on it is released (``matching.release_settlement``: rejected, its
    expected re-opened in the same slot); a proposal naming it is rescinded.
    The next matching pass re-decides both."""
    live = session.execute(
        select(Match)
        .where(Match.posted_id == tx.id, Match.state.in_(matching.LIVE_MATCH_STATES))
        .order_by(Match.created_at, Match.id)
    ).scalars()
    for match in list(live):
        if match.state == "confirmed":
            matching.release_settlement(
                session, match=match, actor_token_id=actor_token_id, reason="transfer.unlink"
            )
            continue
        match.state = "rejected"
        match.decided_by = actor_token_id
        session.flush()
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="match.rescind",
            entity_type="match",
            entity_id=match.id,
            after={
                "expected_id": str(match.expected_id),
                "posted_id": str(tx.id),
                "rescinded_match_ids": [str(match.id)],
                "reason": "transfer.unlink",
            },
        )
        # Its inbox card goes with it (decision 86).
        suggestions_service.on_proposal_closed(
            session,
            match,
            outcome=suggestions_service.STATUS_WITHDRAWN,
            actor_token_id=actor_token_id,
            reason="transfer.unlink",
        )


def unlink_transfer(
    session: Session, *, transfer_id: uuid.UUID, token: ApiToken | None
) -> dict[str, Any]:
    """Undo a merge/absorb: resurrect the voided originals as fresh rows.

    Each original's ``content_hash`` MOVES to its clone (the global unique
    index forbids duplication, and the clone now IS that bank line — import
    idempotency and the pending lifecycle stay exactly right). A
    merge-created transfer is voided with lineage to the resurrected
    outgoing side; a conversion-created transfer survives with its side
    back to unbacked. The emitted ``transfer.unlinked`` payload suppresses
    the AUTO relink paths only.

    An expected the transfer settles is NOT an original (decision 65): it is
    never resurrected (that made a phantom posted copy of the instance).
    When the transfer is voided its settlement goes with it — a confirmed
    match is released exactly as ``unmatch`` would, so the instance is due
    again, and a proposal naming it is rescinded; the matcher re-decides.
    A conversion-created transfer survives, and so does what it settles.

    ``token=None`` is a system actor (the admin delete-account remediation
    reuses this to resurrect counterpart bank rows); scope gating applies
    only to token-bearing callers (precedent: reverse_and_replace_with_specs).
    """
    if token is not None:
        _require_commit(token, "unlinking a transfer")
    tx = matching._load_tx(session, transfer_id)
    if not is_transfer(tx):
        raise LedgerError("only transfers can be unlinked")
    if tx.status not in matching.MATCHABLE_STATUSES:
        raise LedgerError(f"transaction is {tx.status}, not a live transfer")
    _corrected_once_guard(session, tx)
    children = list(
        session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.superseded_by == tx.id,
                Transaction.status == "void",
                linked_original_clause(),
            )
        ).scalars()
    )
    if not children:
        raise LedgerError("nothing to unlink: this transfer has no linked originals")

    prior_statuses = _prior_statuses_for(session, tx.id)
    resurrected: list[tuple[Transaction, str | None]] = []
    for child in children:
        moved_hash = child.content_hash
        child.content_hash = None
        session.flush()  # release the unique index before the clone claims it
        clone = ledger_service._persist_transaction(
            session,
            specs=ledger_service._specs_of(child),
            date=child.date,
            payee_name=child.payee.name if child.payee else None,
            description=child.description,
            requested_status=core.Status(prior_statuses.get(str(child.id), tx.status)),
            origin=core.Origin(child.origin),
            token=token,
            source_fetch_id=child.source_fetch_id,
            content_hash=moved_hash,
        )
        resurrected.append((clone, moved_hash))
        # Review state moves with the row identity: the clone IS that bank
        # line, so it inherits that line's review state (#175, decision 29).
        review_service.copy_to_clone(session, void_id=child.id, clone_id=clone.id)

    transfer_voided = False
    if _was_merge_created(session, tx.id):
        outgoing_clone = next(
            (
                clone
                for clone, _ in resurrected
                if (leg := matching._real_leg(clone)) is not None and Decimal(leg.amount) < 0
            ),
            resurrected[0][0],
        )
        tx.status = "void"
        tx.superseded_by = outgoing_clone.id
        session.flush()
        transfer_voided = True
        _release_settlements_of(session, tx, actor_token_id=token.id if token else None)

    # Resurrections re-enter at the children's dates; a merge-created
    # transfer can post-date its children, so take the min over the whole set
    # (the clones' funnel marks already cover their own dates).
    balances.mark_dirty(session, min([tx.date, *(child.date for child in children)]))
    payload: dict[str, Any] = {
        "transfer_id": str(tx.id),
        "transfer_voided": transfer_voided,
        "resurrected": [
            {"id": str(clone.id), "content_hash": moved_hash} for clone, moved_hash in resurrected
        ],
    }
    if len(resurrected) == 2:
        payload["pair_key"] = matching._pair_key(resurrected[0][0].id, resurrected[1][0].id)
    events.emit(
        session,
        type_="transfer.unlinked",
        entity_type="transaction",
        entity_id=tx.id,
        payload=payload,
        dedup_key=payload.get("pair_key"),
    )
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="transfer.unlink",
        entity_type="transaction",
        entity_id=tx.id,
        after=payload,
    )
    return {
        "transfer_id": str(tx.id),
        "transfer_voided": transfer_voided,
        "resurrected_ids": [str(clone.id) for clone, _ in resurrected],
    }
