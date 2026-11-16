"""Correction chains and bank-line lineages (DESIGN §4/§5, #386 review).

A posted row is never edited in place: a correction writes a reversal of it
and a replacement (``original <- reversal <- replacement``, each linked by
``reverses_id``; ``reverse_and_replace_with_specs`` is the only writer, so
every reversal has its replacement). The row that is current is the chain
HEAD — the one no reversal points at; a reversal never is one. The bank line
lives on in the head of its correction chain (the categorize pass and every
posted edit move it onto a replacement), a pending bank row the bank posted
lives on in its posted successor (``superseded_by``, written by
``feed.reconcile.void_pending``), and an unlinked original lives on in the
clone ``transfers.unlink_transfer`` resurrects it as (``superseded_by`` too).
The bank line's LINEAGE is that history: its root is its first sighting —
the import row, or the pending it posted from.

Transfer pairing asks these walks "is this the same bank line?" — the pool,
the merge guards, the absorb backing and the "not a transfer" memory — so
they cannot drift apart: :func:`ancestry` walks back, :func:`current_rows`
forward. (``schedules.chain_heads`` is the estimates' walk, which also
follows a merged row into its transfer.)
"""

import uuid
from collections.abc import Collection
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.orm import Session, aliased

from purser.models import Account, Match, Posting, Transaction

# A chain is a handful of corrections long; the walks stop here even if a
# cycle could ever be built by hand.
_MAX_HOPS = 64


def chain_head_clause() -> ColumnElement[bool]:
    """SQL: the enclosing query's ``transactions`` row is a chain head — no
    reversal points at it. Written as NOT EXISTS, an anti-join that stays an
    index probe however many corrections the ledger holds (a NOT IN over every
    ``reverses_id`` hashes the whole set, and past work_mem rescans it)."""
    later = aliased(Transaction)
    return ~exists().where(later.reverses_id == Transaction.id)


def _corrections(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{replacement: the row its correction replaced}`` for the ``ids``
    that are replacements — a replacement points at its reversal, the
    reversal at the row it corrected."""
    if not ids:
        return {}
    replacement, reversal = aliased(Transaction), aliased(Transaction)
    return {
        replaced: corrected
        for replaced, corrected in session.execute(
            select(replacement.id, reversal.reverses_id)
            .join(reversal, reversal.id == replacement.reverses_id)
            .where(replacement.id.in_(list(ids)), reversal.reverses_id.is_not(None))
        ).all()
        if corrected is not None
    }


def _one_real_leg(tx_id: Any) -> ColumnElement[bool]:
    """SQL: the transaction ``tx_id`` names posts to exactly one real account
    — a bank line's row, never a transfer."""
    real = (
        select(func.count(Posting.id))
        .join(Account, Account.id == Posting.account_id)
        .where(Posting.transaction_id == tx_id, Account.is_virtual.is_(False))
        .scalar_subquery()
    )
    return real == 1


def _sightings() -> tuple[Any, Any, Any]:
    """``(earlier, later)`` row pairs where a void one-sided row was
    superseded by a one-sided row that carries the same bank line on: a
    pending's posted successor (``feed.reconcile.void_pending``) or an
    unlinked original's clone (``transfers.unlink_transfer``). A merge or an
    absorb supersedes its originals by a transfer (two real legs) and a
    match supersedes its expected by the bank row it settled: neither is an
    earlier sighting of the bank line. Returns the statement and the two
    aliases, for the caller's filter."""
    earlier, later = aliased(Transaction), aliased(Transaction)
    settled = select(Match.id).where(
        Match.expected_id == earlier.id, Match.posted_id == earlier.superseded_by
    )
    stmt = (
        select(earlier.id, later.id)
        .join(later, later.id == earlier.superseded_by)
        .where(
            earlier.status == "void",
            ~settled.exists(),
            _one_real_leg(earlier.id),
            _one_real_leg(later.id),
        )
        .order_by(earlier.created_at, earlier.id)
    )
    return stmt, earlier, later


def _earlier_sightings(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{row: the void row it carries the bank line on from}`` for the
    ``ids`` that have one (:func:`_sightings`)."""
    if not ids:
        return {}
    stmt, _, later = _sightings()
    found: dict[uuid.UUID, uuid.UUID] = {}
    for earlier_id, later_id in session.execute(stmt.where(later.id.in_(list(ids)))).all():
        found.setdefault(later_id, earlier_id)
    return found


def _later_sightings(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{void row: the row that carries its bank line on}`` for the ``ids``
    that have one (:func:`_sightings`)."""
    if not ids:
        return {}
    stmt, earlier, _ = _sightings()
    return {
        earlier_id: later_id
        for earlier_id, later_id in session.execute(stmt.where(earlier.id.in_(list(ids)))).all()
    }


def _replacements(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{corrected row: the replacement its correction wrote}`` for the
    ``ids`` a correction replaced."""
    if not ids:
        return {}
    reversal, replacement = aliased(Transaction), aliased(Transaction)
    return {
        corrected: replaced
        for corrected, replaced in session.execute(
            select(reversal.reverses_id, replacement.id)
            .join(replacement, replacement.reverses_id == reversal.id)
            .where(reversal.reverses_id.in_(list(ids)))
        ).all()
        if corrected is not None
    }


def _forward(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """One step forward along a bank line: a corrected row to its
    replacement, else a void row to the row that carries it on."""
    step = _replacements(session, ids)
    step.update({k: v for k, v in _later_sightings(session, set(ids) - step.keys()).items()})
    return step


def current_rows(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{row: the row that carries its bank line now}`` — the row itself
    when nothing replaced it: forward through corrections and through the
    rows that carry a void row's bank line on (a pending's posted successor,
    an unlinked original's clone), never into a merge or an absorb. The one
    forward walk an explicit confirm, a reject and a card's decision follow
    stale ids with (#386 review), and the hold of a matched row."""
    current = {row_id: row_id for row_id in ids}
    for _ in range(_MAX_HOPS):
        step = _forward(session, set(current.values()))
        if not step:
            break
        current = {row_id: step.get(now, now) for row_id, now in current.items()}
    return current


def correction_ancestry(
    session: Session, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """``{row: the rows its correction chain replaced, nearest first}`` —
    empty for a row that is no replacement. ``ids`` are rows that are not
    reversals (for a reversal the walk has no meaning)."""
    ancestry: dict[uuid.UUID, list[uuid.UUID]] = {row_id: [] for row_id in ids}
    cursor = {row_id: row_id for row_id in ids}
    for _ in range(_MAX_HOPS):
        step = _corrections(session, set(cursor.values()))
        if not step:
            break
        cursor = {row_id: step[current] for row_id, current in cursor.items() if current in step}
        for row_id, current in cursor.items():
            ancestry[row_id].append(current)
    return ancestry


def correction_roots(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{row: the first row of its correction chain}`` (the row itself when
    it is no replacement): the identity a correction never changes."""
    return {
        row_id: (ancestors[-1] if ancestors else row_id)
        for row_id, ancestors in correction_ancestry(session, ids).items()
    }


def ancestry(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """``{row: the earlier rows of its bank line, nearest first}``: back
    through correction chains and through posted successions (a posted bank
    row to the import pending it superseded), to the first sighting of the
    bank line. Empty for a row with neither. ``ids`` are not reversals."""
    earlier: dict[uuid.UUID, list[uuid.UUID]] = {row_id: [] for row_id in ids}
    cursor = {row_id: row_id for row_id in ids}
    for _ in range(_MAX_HOPS):
        frontier = set(cursor.values())
        if not frontier:
            break
        corrected = _corrections(session, frontier)
        step = {**_earlier_sightings(session, frontier - corrected.keys()), **corrected}
        if not step:
            break
        cursor = {row_id: step[current] for row_id, current in cursor.items() if current in step}
        for row_id, current in cursor.items():
            earlier[row_id].append(current)
    return earlier


def lineage_roots(session: Session, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{row: its lineage root}``, the last of its :func:`ancestry` — the
    row itself when it has none."""
    return {
        row_id: (rows[-1] if rows else row_id) for row_id, rows in ancestry(session, ids).items()
    }


def open_pending_successors(
    session: Session, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """``{open pending: the posted row that is its successor in waiting}``
    for the ``ids`` that are open one-sided import pendings with one (#407).

    The reconcile step supersedes a pending by its posted row only when a
    fetch covers the pending's date. A gap-window import (``window_start =
    None``: the outage repair, ``POST /connections/{id}/refetch``) skips that
    step, so its posted rows land beside their still-open pendings, linked
    to nothing until a later sync reconciles them — and the transfer pass
    runs in between. For the "not a transfer" memory such a posted row is
    already the pending's bank line: the row the reconcile step would pick
    (``feed.reconcile.find_posted_replacement``'s rule — an import posted
    row on the pending's account with its exact amount, at most
    ``PENDING_MATCH_WINDOW_DAYS`` away, closest date first, then the earlier
    import), skipping a row that already carries another bank line on (a
    void row superseded into it, :func:`_sightings`) and, among several
    pendings, a row an earlier pending took (the reconcile step's order).
    Read-only: nothing is superseded here, and once the reconcile step runs
    its ``superseded_by`` is the link :func:`ancestry` walks."""
    # Function-local, like every service import of the feed layer.
    from purser.feed.reconcile import PENDING_MATCH_WINDOW_DAYS

    if not ids:
        return {}
    pendings = session.execute(
        select(Transaction.id, Transaction.date, Posting.account_id, Posting.amount)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Transaction.id.in_(list(ids)),
            Transaction.status == "pending",
            Transaction.origin == "import",
            Transaction.superseded_by.is_(None),
            Account.is_virtual.is_(False),
            _one_real_leg(Transaction.id),
        )
        .order_by(Transaction.date, Transaction.created_at, Transaction.id)
    ).all()
    window = timedelta(days=PENDING_MATCH_WINDOW_DAYS)
    found: dict[uuid.UUID, uuid.UUID] = {}
    for pending_id, pending_date, account_id, amount in pendings:
        rows = session.execute(
            select(Transaction.id, Transaction.date, Transaction.created_at)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.status == "posted",
                Transaction.origin == "import",
                Transaction.reverses_id.is_(None),  # neither a reversal nor a replacement
                Posting.account_id == account_id,
                Posting.amount == amount,
                Transaction.date >= pending_date - window,
                Transaction.date <= pending_date + window,
                _one_real_leg(Transaction.id),
            )
        ).all()
        claimed = _earlier_sightings(session, [row.id for row in rows])
        free = [row for row in rows if row.id not in claimed and row.id not in found.values()]
        if free:
            pick = min(
                free, key=lambda row: (abs((row.date - pending_date).days), row.created_at, row.id)
            )
            found[pending_id] = pick.id
    return found


def lineage_members(session: Session, roots: Collection[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{row: root}`` for every row of the lineages these ``roots`` start —
    each root and every row that carried its bank line on since (the
    replacements its corrections wrote, a pending's posted successor, an
    unlinked original's clone): the forward closure of :func:`lineage_roots`.
    A merge or an absorb ends a lineage: the transfer is not a member."""
    member = {root_id: root_id for root_id in roots}
    frontier = set(member)
    for _ in range(_MAX_HOPS):
        if not frontier:
            break
        frontier_next: set[uuid.UUID] = set()
        for previous, row_id in _forward(session, frontier).items():
            if row_id not in member:
                member[row_id] = member[previous]
                frontier_next.add(row_id)
        frontier = frontier_next
    return member
