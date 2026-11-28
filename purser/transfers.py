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
human explicitly unlinked or rejected ("not a transfer", #386) are
suppressed from the AUTO paths only — an explicit confirm or convert can
always link them. Kill switch: ``transfer_auto_link`` (Config).

What the pass leaves unlinked are the confirm-first candidates.
``sync_candidate_suggestions`` runs after every pass (the SimpleFIN and
QIF/CSV imports, the daily job): a candidate whose newer leg is recent gets a ``transfer_pair``
card in the suggestions inbox and its one ``transfer.candidate`` event, and
a card whose pair stopped being a candidate is withdrawn.
"""

import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session, aliased, selectinload

from purser import appsettings, audit, balances, clock, events, matching
from purser import ledger as ledger_service
from purser import lineage as bank_lineage
from purser import suggestions as suggestions_service
from purser.admin import security
from purser.core import ledger as core
from purser.core import matching as core_matching
from purser.ledger import LedgerError
from purser.logging import get_logger
from purser.models import (
    Account,
    ApiToken,
    AuditLog,
    Event,
    Match,
    Posting,
    RawFetch,
    Schedule,
    Suggestion,
    Transaction,
)

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


def _unlinked_pairs(session: Session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """The pairs a human unlinked, as the two rows the unlink resurrected."""
    # pair_key moved to the plaintext dedup_key column (#137): the payload is
    # now encrypted, so a JSONB projection can't read it.
    rows = session.execute(
        select(Event.dedup_key).where(Event.type == "transfer.unlinked")
    ).scalars()
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for key in rows:
        if key:
            low, high = key.split(":")
            pairs.add((uuid.UUID(low), uuid.UUID(high)))
    return pairs


def _suppressed_pairs(session: Session) -> matching.LineagePairs:
    """The pairs no AUTO path may link (#386): the ones a human unlinked,
    and the ones a human rejected as "not a transfer" (a transfer_pair
    suggestion) — by bank line, so a correction of either row (the
    categorize pass corrects most posted rows) or a pending posting since
    leaves the pair suppressed (#386 review). Used by both stages of
    ``plan_auto_links`` — so a transfer_link rule's projected count, decision
    66, still equals what the pass links — by an automatic conversion's
    counterpart and absorb target (``_counterpart_in``,
    ``_absorb_target_for_convert``) and by the candidate list. Explicit
    confirm and convert ignore it: a person can always link a pair."""
    stored: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID | None] = dict.fromkeys(
        _unlinked_pairs(session)
    )
    stored.update(suggestions_service.rejected_transfer_pair_rows(session))
    return matching.lineage_pairs(session, stored)


class SuppressedPairs:
    """``_suppressed_pairs`` kept across one pass (#386 review): the
    categorize pass and a rule's apply-history hand one of these to every
    machine answer they check and apply, instead of re-reading the memory per
    row. Read on first use: a pass that never meets an '[account]' answer
    never reads it.

    Nothing a pass does rejects or unlinks a pair, but a correction does mint
    a replacement row, which the read did not map to its lineage: a rejected
    pair seen through that row would match no key and could be linked. So
    every write that mints a row (``categorize._apply_category``) calls
    ``forget`` with the row it replaced, and when that row is one the memory
    holds, the next check reads the memory again (#497 review). Any other
    row's replacement can match no remembered pair, so the read stands."""

    def __init__(self) -> None:
        self._pairs: matching.LineagePairs | None = None

    def get(self, session: Session) -> matching.LineagePairs:
        if self._pairs is None:
            self._pairs = _suppressed_pairs(session)
        return self._pairs

    def forget(self, replaced: uuid.UUID) -> None:
        if self._pairs is not None and replaced in self._pairs.root_of:
            self._pairs = None


def _unlinked_absorbs(session: Session) -> set[tuple[str, str]]:
    """(transfer_id, bank line) pairs a human explicitly unlinked: the line
    as the clone's content hash, and as the clone's id — a clone of a
    replacement has no hash (#386 review). ``_absorb_unlinked`` reads it."""
    suppressed: set[tuple[str, str]] = set()
    rows = session.execute(select(Event.payload).where(Event.type == "transfer.unlinked")).scalars()
    for payload in rows:
        transfer_id = payload.get("transfer_id")
        if not transfer_id:
            continue
        for resurrected in payload.get("resurrected", []):
            for key in (resurrected.get("content_hash"), resurrected.get("id")):
                if key:
                    suppressed.add((transfer_id, key))
    return suppressed


def _absorb_unlinked(
    suppressed: set[tuple[str, str]], transfer: Transaction, row: Transaction
) -> bool:
    """A human unlinked ``row``'s bank line from ``transfer``."""
    return any(
        (str(transfer.id), key) in suppressed for key in (row.content_hash, str(row.id)) if key
    )


def _conversion_sources(
    session: Session, transfer_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """The bank rows each transfer stands for, beside itself. A posted row
    converted to ``[account]`` is replaced by a transfer whose reversal names
    the original (a pending row converts in place and keeps its id), so an
    absorb into that transfer links the original with the absorbed row: a
    pair a human rejected or unlinked must block it (#386 review)."""
    ids = list(transfer_ids)
    sources: dict[uuid.UUID, set[uuid.UUID]] = {tid: {tid} for tid in ids}
    if not ids:
        return sources
    reversal = aliased(Transaction)
    rows = session.execute(
        select(Transaction.id, reversal.reverses_id)
        .join(reversal, reversal.id == Transaction.reverses_id)
        .where(Transaction.id.in_(ids), reversal.reverses_id.is_not(None))
    ).all()
    for transfer_id, original_id in rows:
        if original_id is not None:
            sources[transfer_id].add(original_id)
    return sources


def _pair_suppressed(
    row_id: uuid.UUID,
    transfer_id: uuid.UUID,
    sources: dict[uuid.UUID, set[uuid.UUID]],
    suppressed_pairs: matching.LineagePairs,
) -> bool:
    """Absorbing ``row_id`` into ``transfer_id`` would link a suppressed pair."""
    return any(
        (source, row_id) in suppressed_pairs for source in sources.get(transfer_id, {transfer_id})
    )


@dataclass
class _OpenLeg:
    """An unbacked side of a live transfer — absorb phase target."""

    transfer: Transaction
    account_id: uuid.UUID
    amount: Decimal
    counterparty_account_id: uuid.UUID
    counterparty_name: str


def _open_transfer_legs(session: Session, *, account_id: uuid.UUID | None = None) -> list[_OpenLeg]:
    """Legs of live chain-head transfers that no voided import backs yet —
    of every transfer, or only of those with a leg on ``account_id`` (one
    conversion's absorb target, #386 review: not the whole ledger's).

    Loads live chain-head transactions and filters to the PLAIN transfer
    shape (two real legs that cancel, no virtual) in Python — transfers are a
    small slice of the ledger, and this keeps it to two queries for the whole
    phase. A split loan payment is never an absorb target: its unequal legs
    were both claimed from bank rows when it was confirmed.
    """
    rows = list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(
                Transaction.status.in_(matching.MATCHABLE_STATUSES),
                Transaction.superseded_by.is_(None),
                bank_lineage.chain_head_clause(),
                ~matching.trade_cash_clause(),
                *(
                    ()
                    if account_id is None
                    else (
                        Transaction.id.in_(
                            select(Posting.transaction_id).where(Posting.account_id == account_id)
                        ),
                    )
                ),
            )
        ).scalars()
    )
    transfers = [t for t in rows if is_transfer_shaped(t)]
    if not transfers:
        return []
    backed: set[tuple[uuid.UUID, uuid.UUID]] = set()  # (transfer_id, account_id)
    # Backing means "this bank line is accounted for": a child still merged
    # into the transfer (an unlinked one points at its clone instead, #386
    # review) that IS a bank line — a row still holding its content hash, or
    # a hashless row whose lineage holds an import row (a replacement: the
    # hash stays on the corrected original; a clone of one). An import row
    # without its hash was unlinked before unlink re-pointed originals: it
    # backs nothing, as before. Voided manual originals never back (the
    # bank's own rows for that transfer should still be absorbed), and an
    # expected the transfer settles is no original at all.
    children = list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(
                Transaction.superseded_by.in_([t.id for t in transfers]),
                Transaction.status == "void",
                linked_original_clause(),
            )
        ).scalars()
    )
    lines = matching.bank_line_ids(
        session, [c for c in children if c.content_hash is None and c.origin != "import"]
    )
    for child in (c for c in children if c.content_hash is not None or c.id in lines):
        for p in child.postings:
            if not p.account.is_virtual and child.superseded_by is not None:
                backed.add((child.superseded_by, p.account_id))
    backed |= _converted_bank_lines(session, transfers)
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


def _emit_converted(
    session: Session, *, transfer: Transaction, own_account_id: uuid.UUID, target: Account
) -> None:
    """``transfer.converted``: a row became a transfer to ``target`` through
    the '[account]' sugar. ``dedup_key`` is the account the row was on, in
    plain text — the durable record of which side a pending row converted in
    place is the bank's own (``_converted_bank_lines``; the audit row is
    pruned, events are not, #386 review)."""
    own = session.get(Account, own_account_id)
    events.emit(
        session,
        type_="transfer.converted",
        entity_type="transaction",
        entity_id=transfer.id,
        payload={
            "transfer_id": str(transfer.id),
            "account": own.path if own is not None else None,
            "counterparty_account": target.path,
        },
        dedup_key=str(own_account_id),
    )


def _converted_bank_lines(
    session: Session, transfers: list[Transaction]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """(transfer_id, account_id) legs a converted bank row backs itself (#386
    review): a bank row converted to '[account]' IS that account's bank line,
    so its own side was never open — reading it open voided the next real
    same-amount row on that account into the transfer, and that row was gone
    from the ledger. A manual row converted the same way backs nothing: the
    bank's own row for it should still be absorbed.

    - Posted, the conversion is a reversal + a transfer-shaped replacement;
      the reversal names the converted row (``_conversion_sources``).
    - Pending, the row is converted in place: the transfer is the bank row
      itself, and its own side is the account it was on (``_own_sides``).
      A QIF/CSV transfer, imported whole, backs nothing here: its legs are
      backed by what is absorbed into them."""
    if not transfers:
        return set()
    sides = {t.id: {p.account_id for p in t.postings} for t in transfers}
    backed: set[tuple[uuid.UUID, uuid.UUID]] = set()
    sources = _conversion_sources(session, list(sides))
    originals = {source for t_id, ids in sources.items() for source in ids if source != t_id}
    if originals:
        rows = list(
            session.execute(
                select(Transaction)
                .options(selectinload(Transaction.postings).selectinload(Posting.account))
                .where(Transaction.id.in_(originals))
            ).scalars()
        )
        bank_lines = matching.bank_line_ids(session, rows)
        account_of = {
            tx.id: leg.account_id
            for tx in rows
            if tx.id in bank_lines and (leg := matching._real_leg(tx)) is not None
        }
        for t_id, ids in sources.items():
            for source in ids - {t_id}:
                account = account_of.get(source)
                if account is not None and account in sides[t_id]:
                    backed.add((t_id, account))
    in_place = [t for t in transfers if t.origin == "import"]
    if in_place:
        own = _own_sides(session, in_place, sides)
        for t in in_place:
            kept = own.get(t.id)
            if kept is not None:
                backed |= {(t.id, account) for account in kept & sides[t.id]}
    return backed


def _own_sides(
    session: Session, in_place: list[Transaction], sides: dict[uuid.UUID, set[uuid.UUID]]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """The account each import-origin transfer was on before a conversion
    made it one, by what records it (#386 review): its newest
    ``transfer.converted`` event (durable); for a conversion older than that
    event, its newest ``transfer.convert`` audit row (the account before and
    after) while the audit retention keeps it; failing both, the one leg on
    an account of the connection its feed row came from. A transfer none of
    these names — a QIF/CSV transfer imported whole, or an old conversion
    whose two accounts share a connection — backs nothing."""
    ids = [t.id for t in in_place]
    own: dict[uuid.UUID, set[uuid.UUID]] = {}
    for entity_id, account in session.execute(
        select(Event.entity_id, Event.dedup_key)
        .where(Event.type == "transfer.converted", Event.entity_id.in_(ids))
        .order_by(Event.at.desc(), Event.id.desc())
    ).all():
        if entity_id is not None and account and entity_id not in own:
            own[entity_id] = {uuid.UUID(account)}

    def accounts(shape: dict[str, Any] | None) -> set[uuid.UUID]:
        """The accounts an audit row's transaction shape posts to."""
        return {uuid.UUID(p["account_id"]) for p in (shape or {}).get("postings", [])}

    rest = [i for i in ids if i not in own]
    if rest:
        for entity_id, before_after in session.execute(
            select(AuditLog.entity_id, AuditLog.before_after)
            .where(
                AuditLog.action == "transfer.convert",
                AuditLog.entity_type == "transaction",
                AuditLog.entity_id.in_(rest),
            )
            .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        ).all():
            if entity_id is None or entity_id in own or not before_after:
                continue
            own[entity_id] = accounts(before_after.get("before")) & accounts(
                before_after.get("after")
            )
    fed = [t for t in in_place if t.id not in own and t.source_fetch_id is not None]
    if fed:
        connection_of = dict(
            session.execute(
                select(RawFetch.id, RawFetch.connection_id).where(
                    RawFetch.id.in_({t.source_fetch_id for t in fed})
                )
            ).all()
        )
        on_connection: dict[uuid.UUID, uuid.UUID | None] = dict(
            session.execute(
                select(Account.id, Account.connection_id).where(
                    Account.id.in_({a for t in fed for a in sides[t.id]})
                )
            ).all()
        )
        for t in fed:
            assert t.source_fetch_id is not None  # ``fed`` holds feed rows only
            connection = connection_of.get(t.source_fetch_id)
            mine = {
                a
                for a in sides[t.id]
                if connection is not None and on_connection.get(a) == connection
            }
            if len(mine) == 1:
                own[t.id] = mine
    return own


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
    # Locked and reloaded before anything changes: a concurrent correction or
    # merge of the row, or of the transfer, is seen (#386 review).
    ledger_service.lock_rows(session, (row, transfer))
    if (
        row.status not in matching.MATCHABLE_STATUSES
        or row.superseded_by is not None
        or transfer.status not in matching.MATCHABLE_STATUSES
        or transfer.superseded_by is not None
    ):
        raise ledger_service.RowChanged(f"transaction {row.id} can no longer be absorbed")
    ledger_service.refuse_corrected(session, row)
    ledger_service.refuse_corrected(session, transfer)
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
    # Unlinked or rejected ("not a transfer", #386): both stages skip them,
    # as pairs and as absorbs into a transfer converted from one side.
    suppressed_pairs = _suppressed_pairs(session)
    sources = (
        _conversion_sources(
            session, {leg.transfer.id for legs in open_legs.values() for leg in legs}
        )
        if open_legs and suppressed_pairs
        else {}
    )
    # Open instances of active transfer schedules, taken from this one list by
    # both stages: each covers one merge at most.
    active = _transfer_schedule_ids(session)
    instances = _open_instances(session, active)

    def rest(taken: set[uuid.UUID]) -> list[Transaction]:
        return [r for r in pool if r.id not in taken]

    def unheld(rows: list[Transaction]) -> list[Transaction]:
        return [r for r in rows if r.id not in holds]

    absorbs = _plan_absorbs(
        unheld(pool),
        open_legs,
        declared,
        suppressed_absorbs,
        widened=False,
        suppressed_pairs=suppressed_pairs,
        sources=sources,
    )
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
            unheld(rest(taken)),
            open_legs,
            declared,
            suppressed_absorbs,
            widened=True,
            suppressed_pairs=suppressed_pairs,
            sources=sources,
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
    suppressed_pairs: matching.LineagePairs = matching.NO_PAIRS,
    sources: dict[uuid.UUID, set[uuid.UUID]] | None = None,
) -> list[PlannedAbsorb]:
    """Rows claimed by existing transfers with an unbacked side. Standard:
    within ±3 days, on a name signal or a declaration whose window covers the
    gap. Widened: farther apart, on a declaration only. Claimed legs leave
    ``open_legs``. A row is never absorbed into a transfer converted from the
    other side of a pair a human rejected or unlinked (``suppressed_pairs``,
    ``sources`` = ``_conversion_sources``; #386 review)."""
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
            if _absorb_unlinked(suppressed, leg.transfer, row):
                continue
            if suppressed_pairs and _pair_suppressed(
                row.id, leg.transfer.id, sources or {}, suppressed_pairs
            ):
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
    suppressed: matching.LineagePairs,
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

    # A suppressed pair (unlinked or rejected) is ruled out inside the
    # pairing, so it never consumes its rows: they stay free to pair with
    # their real counterparts, in this stage or the widened one (#386 review).
    pairs, tx_of = matching.detect_transfer_pairs_for(
        session, pool, holds=holds, window=window, suppressed=suppressed
    )
    paired: set[uuid.UUID] = set()
    planned: list[PlannedMerge] = []
    for pair in pairs:
        paired.update((pair.outgoing_id, pair.incoming_id))
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
    # The trade-cash lineage (decision 103), read once for the pass: its
    # merges and re-matches neither make nor move a trade's cash (the pool
    # leaves trade cash out through matching.trade_cash_clause).
    claimed = ledger_service.trade_cash_lineage_set(session)
    plan = plan_auto_links(session, declared)

    # A link a declaration made is one application of that rule (#314), the
    # way a categorize or rename rule counts each row it shapes: the rule the
    # link's signal names, once per link, in both stages and both phases.
    # Counted here, where links are made, so the activation confirm's
    # projection (the same plan) applies nothing; unlink takes nothing back.
    # A row a concurrent correction or merge changed after the plan read it
    # is refused before anything is written (RowChanged, under the row lock):
    # the pass leaves it and goes on, and the next pass sees it as it is.
    for absorb in plan.absorbs:
        signal = "name" if absorb.rule_id is None else f"rule:{absorb.rule_id}"
        try:
            # Never wait on a row another writer holds (a sale linking it):
            # this pass may hold the balance watermark that writer needs.
            ledger_service.lock_transaction(session, absorb.row.id, skip_locked=True)
            _absorb(session, row=absorb.row, leg=absorb.leg, signal=signal)
        except (ledger_service.RowChanged, ledger_service.RowBusy) as exc:
            # Changed since the plan (corrected, merged, voided) or held by
            # another writer: left as it is, the next pass sees it anew.
            log.info("transfer absorb skipped", row_id=str(absorb.row.id), reason=str(exc))
            continue
        stats["transfers_absorbed"] += 1
        if absorb.rule_id is not None:
            rules_service.mark_applied(session, [absorb.rule_id])

    for merge in plan.merges:
        # The merge carries any match on its rows (decision 65); a covered
        # instance still open is then settled by the merge at once, so the
        # forecast never counts it beside the posted legs.
        try:
            matching._merge_transfer_pair(
                session,
                outgoing=merge.outgoing,
                incoming=merge.incoming,
                token=None,
                origin=core.Origin.SYSTEM,
                action="transfer.auto_link",
                event_type="transfer.auto_linked",
                signal=merge.signal,
                claimed=claimed,
                skip_busy=True,
            )
        except (ledger_service.RowChanged, ledger_service.RowBusy) as exc:
            # A changed or held row: the next pass pairs it on what commits.
            log.info("transfer auto-link skipped", reason=str(exc))
            continue
        stats["transfers_auto_linked"] += 1
        if merge.rule_id is not None:
            rules_service.mark_applied(session, [merge.rule_id])
        if merge.settles is not None:
            instance = matching._load_tx(session, merge.settles)
            if instance.status == "expected":
                matching._match_expected(session, instance, claimed=claimed)

    if stats["transfers_absorbed"] or stats["transfers_auto_linked"]:
        log.info("transfer auto-link pass", **stats)
    return stats


# ---------------------------------------------------------------------------
# Confirm-first candidates and their inbox cards (#386, decision 102)
# ---------------------------------------------------------------------------


_INCOME_CREDIT = re.compile(
    r"\b(?:cash\s?back|(?:cash\s+)?rewards?\s+credit|cash\s+rewards?|refund|dividends?|"
    r"interest\s+(?:credit|paid|earned))\b",
    re.IGNORECASE,
)


def _income_credit(tx: Transaction) -> bool:
    legs = [p for p in tx.postings if not p.account.is_virtual]
    return (
        len(legs) == 1
        and Decimal(legs[0].amount) > 0
        and bool(_INCOME_CREDIT.search(_signal_text(tx)))
    )


def candidate_pairs(
    session: Session,
) -> list[tuple[core_matching.TransferCandidate, Transaction, Transaction]]:
    """Every transfer candidate over the whole history, as ``(pair,
    outgoing, incoming)``: the opposite-amount pairs the transfer pool forms
    inside the standard window (``matching.transfer_pool``, so held legs
    pair with their expected's other side, decision 65). A pair a human
    unlinked or rejected is never formed — ruled out inside the pairing, as
    in the plan, so its rows can still pair with their real counterparts.

    Posted rows pair first, exactly as the plan pairs them (its pool is
    posted rows only, §5); pending rows then pair only from what is left, for
    the register's list. A pending row never takes a posted row's
    counterpart: posted pairs are what the auto paths act on and what cards
    are made of, so a pending row arriving must not withdraw one (#386
    review). Pairs are found in (date, created_at, id) order, the plan's
    order, so the same ledger always yields the same pairs and a card keeps
    its pair; they come back in the outgoing row's order."""
    rows, holds = matching.transfer_pool(session)
    # Positive merchant refunds and earned credits are not evidence of an
    # own-account transfer. Filter before pairing so a coincidental amount
    # cannot steal the actual counterpart. Explicit linking remains available.
    rows = [r for r in rows if not _income_credit(r)]
    rows.sort(key=lambda t: (t.date, t.created_at, t.id))
    suppressed = _suppressed_pairs(session)
    pairs, tx_of = matching.detect_transfer_pairs_for(
        session, [r for r in rows if r.status == "posted"], holds=holds, suppressed=suppressed
    )
    paired = {row_id for pair in pairs for row_id in (pair.outgoing_id, pair.incoming_id)}
    rest = [r for r in rows if r.id not in paired]
    if any(r.status == "pending" for r in rest):
        later, more = matching.detect_transfer_pairs_for(
            session, rest, holds=holds, suppressed=suppressed
        )
        tx_of.update(more)
        pairs += [
            pair
            for pair in later
            if "pending" in (tx_of[pair.outgoing_id].status, tx_of[pair.incoming_id].status)
        ]
    order = {row.id: index for index, row in enumerate(rows)}
    pairs.sort(key=lambda pair: order[pair.outgoing_id])
    return [(pair, tx_of[pair.outgoing_id], tx_of[pair.incoming_id]) for pair in pairs]


def sync_candidate_suggestions(
    session: Session, *, actor_token_id: uuid.UUID | None = None
) -> dict[str, int]:
    """The inbox side of the candidate list, run after every auto-link pass
    (the SimpleFIN and QIF/CSV imports, the daily job). The caller owns the
    commit.

    - A candidate whose rows are both POSTED and whose newer leg is at most
      ``suggestions.TRANSFER_PAIR_LOOKBACK_DAYS`` old gets its
      ``transfer.candidate`` event (once per pair, ever) and a
      ``transfer_pair`` card — created, re-opened if it was withdrawn, or
      refreshed. Older pairs stay in the register's whole-history list. A
      pair with a pending row waits, as the auto paths do (§5): the bank
      posts a pending row as a new row, so its card would only churn —
      replaced, or withdrawn when the posted pair auto-links (#386 review).
    - An open card whose pair is no longer card-worthy at all (a row linked,
      absorbed, matched to a bill, corrected or voided; the pair unlinked; a
      closer row paired instead) is withdrawn. A card that merely aged past
      the window stays until it is decided: the window bounds what lands in
      the inbox, not how long a person may take.
    - Rejected pairs are suppressed, so they get neither."""
    stats = {"transfer_pair_cards_created": 0, "transfer_pair_cards_withdrawn": 0}
    cutoff = clock.ledger_today() - timedelta(days=suggestions_service.TRANSFER_PAIR_LOOKBACK_DAYS)
    posted = [
        (pair, outgoing, incoming)
        for pair, outgoing, incoming in candidate_pairs(session)
        # a card waits until both rows post
        if outgoing.status == "posted" and incoming.status == "posted"
    ]
    keys = matching.card_keys(session, [(p.outgoing_id, p.incoming_id) for p, _, _ in posted])
    live: set[str] = set()
    for pair, outgoing, incoming in posted:
        live.add(
            suggestions_service.transfer_pair_fingerprint(
                keys[(pair.outgoing_id, pair.incoming_id)]
            )
        )
        if max(outgoing.date, incoming.date) < cutoff:
            continue
        matching._emit_transfer_candidate_once(session, pair)
        _, created = suggestions_service.upsert_transfer_pair_suggestion(
            session,
            outgoing=outgoing,
            incoming=incoming,
            amount=pair.amount,
            gap_days=pair.date_gap_days,
            actor_token_id=actor_token_id,
        )
        if created:
            stats["transfer_pair_cards_created"] += 1
    stale = [
        row
        for fingerprint, row_id in suggestions_service.open_transfer_pair_cards(session).items()
        if fingerprint not in live and (row := session.get(Suggestion, row_id)) is not None
    ]
    for row in stale:
        # False when a person decided the card since it was read (#497 review).
        if suggestions_service.withdraw_transfer_pair(
            session, row, actor_token_id=actor_token_id, reason="no_longer_candidate"
        ):
            stats["transfer_pair_cards_withdrawn"] += 1
    if stats["transfer_pair_cards_created"] or stats["transfer_pair_cards_withdrawn"]:
        log.info("transfer candidate cards", **stats)
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
    if tx.split_account_id is not None:
        # A split is never a transfer, whatever its shape (decision 118).
        return None
    if any(p.account is None for p in tx.postings):
        return None
    return core.transfer_shape(
        [core.Leg(p.account_id, Decimal(p.amount), p.account.is_virtual) for p in tx.postings]
    )


def split_lines(tx: Transaction) -> int:
    """How many lines ``tx`` has when it is a split transaction — the rows
    carrying the split marker (DESIGN §3.3, decisions 87 and 118), category
    and ``[Account]`` lines alike — and 0 for every other row. A split is
    edited only through ``splits.split_transaction``: the categorize pass, a
    categorize rule's apply-history, the AI classifier and the transfer pool
    leave it alone."""
    if tx.split_account_id is None or any(p.account is None for p in tx.postings):
        return 0
    return core.split_line_count(
        [core.Leg(p.account_id, Decimal(p.amount), p.account.is_virtual) for p in tx.postings],
        tx.split_account_id,
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


class MachineAnswerRefused(LedgerError):
    """An automatic '[account]' answer that would make a transfer of a pair a
    person rejected or unlinked (#386 review): no answer, never a conversion."""


@dataclass(frozen=True)
class _Pick:
    """What a conversion pairs with — the transfer to absorb into, or the
    counterpart to merge with — and whether the machine path left a
    candidate out only because a person rejected or unlinked that pair."""

    chosen: Transaction | None
    refused: bool = False


def _absorb_target_for_convert(
    session: Session,
    *,
    row: Transaction,
    real: Posting,
    target_account_id: uuid.UUID,
    auto_pairing: bool = False,
    pairs: SuppressedPairs | None = None,
) -> _Pick:
    """An existing live transfer connecting exactly (row's account <-> target)
    whose row-side leg is unbacked and matches amount + window. Explicit user
    intent: no name signal, no unlink suppression.

    ``auto_pairing`` is the machine path (DESIGN §11 rule 5): the model named
    the account, so the *pairing* must follow ``auto_link_pass``'s rules
    instead — posted rows only, and a pair a human unlinked or rejected stays
    unlinked, including through a transfer converted from its other side
    (#386 review).
    """
    amount = Decimal(real.amount)
    in_reach = [
        leg
        for leg in _open_transfer_legs(session, account_id=real.account_id)
        if leg.account_id == real.account_id
        and leg.amount == amount
        and leg.counterparty_account_id == target_account_id
        and abs(row.date - leg.transfer.date) <= _WINDOW
        and leg.transfer.id != row.id
    ]
    candidates, refused = in_reach, False
    if auto_pairing and in_reach:
        suppressed = _unlinked_absorbs(session)
        suppressed_pairs = (pairs or SuppressedPairs()).get(session)
        sources = (
            _conversion_sources(session, {leg.transfer.id for leg in in_reach})
            if suppressed_pairs
            else {}
        )
        rejected = {
            leg.transfer.id
            for leg in in_reach
            if suppressed_pairs
            and _pair_suppressed(row.id, leg.transfer.id, sources, suppressed_pairs)
        }
        refused = bool(rejected)
        candidates = [
            leg
            for leg in in_reach
            if row.status == "posted"
            and leg.transfer.status == "posted"
            and not _absorb_unlinked(suppressed, leg.transfer, row)
            and leg.transfer.id not in rejected
        ]
    if not candidates:
        return _Pick(None, refused=refused)
    best = min(
        candidates,
        key=lambda leg: (
            abs((row.date - leg.transfer.date).days),
            leg.transfer.created_at,
            leg.transfer.id,
        ),
    )
    return _Pick(best.transfer, refused=refused)


def _counterpart_in(
    session: Session,
    *,
    account_id: uuid.UUID,
    amount: Decimal,
    row: Transaction,
    auto_pairing: bool = False,
    pairs: SuppressedPairs | None = None,
) -> _Pick:
    """A live one-sided row in the target account with the opposite amount
    inside the window — the merge case of an explicit conversion.

    ``auto_pairing`` narrows the pool to what ``auto_link_pass`` would accept
    (DESIGN §11 rule 5): rows of the transfer pool only (``matching.
    transfer_pool`` — never a manual row, nor a replacement in which a
    person or an agent set a category: merging it would discard that
    decision, #386 review),
    **posted rows only** — a bank-pending row can come back posted under a
    different description, and consuming it in a merge first turns that
    rehash into a duplicated leg — and pairs a human unlinked or rejected
    stay unlinked (#386).

    A counterpart the matcher holds for an expected transfer to ``row``'s
    account is still a counterpart (decision 65, the auto-link pool's rule):
    without it, converting the late side made a second copy of the held
    leg. ``row`` must not hold a match itself — one merge carries one.
    """
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
            bank_lineage.chain_head_clause(),
            ~matching.trade_cash_clause(),
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
    # Matched — or held — through the correction chain, as the pool reads it
    # (#386 review): a match stays on the corrected original, so a
    # replacement whose original settles a bill is that bill's bank row, and
    # a held one comes back below with its hold.
    matched = matching._live_matches_on_lineage(session, [*one_sided, row])
    one_sided = [t for t in one_sided if t.id not in matched]
    row_leg = matching._real_leg(row)
    row_is_held = row.id in matched
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
        # _transfer_holds holds chain heads only, so a corrected row never
        # reaches here.
        one_sided += held
    refused = False
    if auto_pairing:
        pool, _ = matching.transfer_pool(session, only=[t.id for t in one_sided])
        in_pool = {t.id for t in pool}
        suppressed = (pairs or SuppressedPairs()).get(session)
        eligible = [t for t in one_sided if t.id in in_pool]
        refused = any((row.id, t.id) in suppressed for t in eligible)
        one_sided = [
            t
            for t in eligible
            if row.status == "posted" and t.status == "posted" and (row.id, t.id) not in suppressed
        ]
    if not one_sided:
        return _Pick(None, refused=refused)
    return _Pick(
        min(one_sided, key=lambda t: (abs((row.date - t.date).days), t.created_at, t.id)),
        refused=refused,
    )


def machine_answer_refused(
    session: Session,
    tx: Transaction,
    target_id: uuid.UUID,
    *,
    pairs: SuppressedPairs | None = None,
) -> bool:
    """Whether an automatic answer of ``target_id`` for ``tx`` — the
    categorize pass's rule, memory or model tier, a categorize rule's
    apply-history — must count as no answer (#386 review): ``target_id`` is a
    real account whose only counterpart for this row (a one-sided row, or the
    open leg of a transfer converted from one) forms a pair a person said is
    not a transfer, or unlinked. The automated passes ask first, as they do
    about the currency (``categorize.fits_currency``), so a refused answer
    leaves the row for the next tier instead of failing the pass on it."""
    target = session.get(Account, target_id)
    real = matching._real_leg(tx)
    if (
        target is None
        or target.is_virtual
        or real is None
        or real.account_id == target.id
        or tx.status not in matching.MATCHABLE_STATUSES
    ):
        return False
    absorb = _absorb_target_for_convert(
        session, row=tx, real=real, target_account_id=target.id, auto_pairing=True, pairs=pairs
    )
    if absorb.chosen is not None:
        return False
    counterpart = _counterpart_in(
        session,
        account_id=target.id,
        amount=-Decimal(real.amount),
        row=tx,
        auto_pairing=True,
        pairs=pairs,
    )
    return counterpart.chosen is None and (absorb.refused or counterpart.refused)


def convert_to_transfer(
    session: Session,
    *,
    tx: Transaction,
    target_ref: str,
    token: ApiToken | None,
    auto_pairing: bool = False,
    pairs: SuppressedPairs | None = None,
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
    ``auto_link_pass`` would accept: rows of the transfer pool, posted rows
    only, unlinked and rejected pairs left alone. When those were its only
    candidates, the answer is refused (``MachineAnswerRefused``, asked first
    by ``machine_answer_refused``) instead of converting in place: "not a
    transfer" means automation never makes either row a transfer to the
    other's account (#386 review). A corrected original is refused before
    any of it.
    """
    target = ledger_service.resolve_account(session, target_ref)
    if target.is_virtual:
        raise LedgerError(f"{target.path} is a category — transfers connect two real accounts")
    # Before anything that absorbs or merges the row (#386 review): a
    # corrected original absorbed or merged would leave its replacement live
    # beside the transfer, the money counted twice. Locked first, so a
    # concurrent correction or merge of the row is seen. The machine path
    # never waits on a row another writer holds (a sale linking it, decision
    # 103): it skips it (RowBusy) for its next pass.
    if auto_pairing:
        ledger_service.lock_transaction(session, tx.id, skip_locked=True)
    ledger_service.lock_rows(session, [tx])
    if tx.status == core.Status.VOID or tx.superseded_by is not None:
        raise ledger_service.RowChanged(f"transaction {tx.id} is {tx.status}")
    ledger_service.refuse_corrected(session, tx)
    # A trade's cash, or the bank row a trade linked, moves only with its
    # trade (decision 103): no absorb, merge or in-place conversion takes it.
    ledger_service.refuse_trade_cash_lineage(session, tx, what="become a transfer")

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
        absorb = _absorb_target_for_convert(
            session,
            row=tx,
            real=real,
            target_account_id=target.id,
            auto_pairing=auto_pairing,
            pairs=pairs,
        )
        if absorb.chosen is not None:
            _require_commit(token, "absorbing into an existing transfer")
            _absorb_row(
                session,
                row=tx,
                transfer=absorb.chosen,
                account_id=real.account_id,
                amount=amount,
                signal=signal,
                actor_token_id=token.id if token else None,
            )
            return absorb.chosen

        counterpart = _counterpart_in(
            session,
            account_id=target.id,
            amount=-amount,
            row=tx,
            auto_pairing=auto_pairing,
            pairs=pairs,
        )
        if counterpart.chosen is not None:
            _require_commit(token, "merging a transfer pair")
            outgoing, incoming = (
                (tx, counterpart.chosen) if amount < 0 else (counterpart.chosen, tx)
            )
            return matching._merge_transfer_pair(
                session,
                outgoing=outgoing,
                incoming=incoming,
                token=token,
                card_reason="transfer.convert",
                skip_busy=auto_pairing,
            )
        if absorb.refused or counterpart.refused:
            # "Not a transfer" means automation never makes either row a
            # transfer to the other's account (#386 review): converted in
            # place, the row would carry that account a second copy of the
            # money its rejected counterpart already holds there.
            raise MachineAnswerRefused(
                f"{target.path} holds this row's counterpart of a pair a person said is not"
                " a transfer"
            )

    if tx.status == core.Status.POSTED:
        _require_commit(token, "converting a posted transaction")
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
        replacement = ledger_service.reverse_and_replace_with_specs(
            session, tx=tx, token=token, replacement_specs=specs, action="transfer.convert"
        )
        _emit_converted(
            session, transfer=replacement, own_account_id=real.account_id, target=target
        )
        return replacement

    before = ledger_service._tx_audit_shape(tx, ledger_service._specs_of(tx))
    # Assign the relationship (not just the FK): the session keeps objects
    # live across commit (expire_on_commit=False), so serializers reading
    # posting.account must see the new account immediately.
    category_leg.account = target
    session.flush()
    # The category leg just became a REAL leg: ``target`` gains this row.
    balances.mark_dirty(session, tx.date)
    _audit_convert(session, tx=tx, token=token, before=before)
    _emit_converted(session, transfer=tx, own_account_id=real.account_id, target=target)
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
    ledger_service.refuse_system_account(category)
    kept = legs[kept_account.id]
    ledger_service.check_category_currency(kept.currency, category)
    other = next(p for p in tx.postings if p is not kept)

    if tx.status == core.Status.POSTED:
        _require_commit(token, "recategorizing a posted transfer")
        ledger_service.refuse_corrected(session, tx)
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


def resurrect_original(
    session: Session, child: Transaction, *, status: core.Status, token: ApiToken | None
) -> tuple[Transaction, str | None]:
    """Bring one voided original back as a fresh clone carrying ``status``
    (its status before it was merged): same postings, date, payee, text,
    origin and source fetch, its ``content_hash`` MOVED onto the clone.
    Shared by unlink and un-split (decision 118).
    Returns the clone and the hash it took over."""
    moved_hash = child.content_hash
    child.content_hash = None
    session.flush()  # release the unique index before the clone claims it
    clone = ledger_service._persist_transaction(
        session,
        specs=ledger_service._specs_of(child),
        date=child.date,
        payee_name=child.payee.name if child.payee else None,
        description=child.description,
        requested_status=status,
        origin=core.Origin(child.origin),
        token=token,
        source_fetch_id=child.source_fetch_id,
        content_hash=moved_hash,
    )
    # The clone carries the original's bank line on (#386 review): the
    # original now points at it, as a posted pending points at its
    # successor. So the clone's lineage reaches the original's — it stays a
    # bank line for pairing even without a hash (a replacement never had
    # one) — and the original no longer reads as merged into what it backed.
    child.superseded_by = clone.id
    session.flush()
    return clone, moved_hash


def unlink_transfer(
    session: Session, *, transfer_id: uuid.UUID, token: ApiToken | None
) -> dict[str, Any]:
    """Undo a merge/absorb: resurrect the voided originals as fresh rows.

    Each original's ``content_hash`` MOVES to its clone (the global unique
    index forbids duplication, and the clone now IS that bank line — import
    idempotency and the pending lifecycle stay exactly right), and the
    original's ``superseded_by`` moves to the clone as well: the clone
    carries its bank line on (``lineage``), and the original is no longer a
    linked original of the transfer, backing nothing. A
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
    ledger_service.refuse_corrected(session, tx)
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
        resurrected.append(
            resurrect_original(
                session,
                child,
                status=core.Status(prior_statuses.get(str(child.id), tx.status)),
                token=token,
            )
        )

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
