"""Load a position's lots as the pure core reads them (``purser.core.lots``,
issue #393, decision 107).

The projection, the event writes and the capture planner all read lots
through here, so a position's lots mean the same thing to each of them:
voided lots are never loaded, and ``core.lots.live`` keeps only the lots
whose opening event is effective and the dispositions whose event is.
"""

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from purser import crypto
from purser.core import holdings as core
from purser.core import lots as core_lots
from purser.models import Lot, LotDisposition

REF_NAMESPACE = "lot_ref"
PLAN_NAMESPACE = "lot_plan"
GRANT_NAMESPACE = "award_grant"


def normalize_ref(raw: str) -> str:
    """A broker's lot or grant reference as compared: stripped, otherwise
    verbatim (references are opaque and may be case-sensitive)."""
    out = raw.strip()
    if not out:
        raise ValueError("a reference cannot be empty")
    if len(out) > 200:
        raise ValueError("a reference is at most 200 characters")
    return out


def normalize_plan(raw: str) -> str:
    """A plan label as compared: whitespace collapsed, case folded. The label
    is stored as given (encrypted)."""
    out = " ".join(raw.split()).casefold()
    if not out:
        raise ValueError("a plan label cannot be empty")
    if len(out) > 200:
        raise ValueError("a plan label is at most 200 characters")
    return out


def ref_index(ref: str, namespace: str = REF_NAMESPACE) -> str:
    """The blind index of a reference: HMAC under the blind-index DEK with a
    namespace, so a lot reference and a grant id never collide and neither
    equals a payee's index."""
    return crypto.blind_index(f"{namespace}\x1f{normalize_ref(ref)}")


def plan_index(plan: str) -> str:
    return crypto.blind_index(f"{PLAN_NAMESPACE}\x1f{normalize_plan(plan)}")


def lot_fact(row: Lot) -> core_lots.LotFact:
    return core_lots.LotFact(
        id=row.id,
        acquired_on=row.acquired_on,
        effective_on=row.effective_on,
        quantity=Decimal(row.quantity),
        cost_basis=Decimal(row.cost_basis) if row.cost_basis is not None else None,
        basis_per_share=(Decimal(row.basis_per_share) if row.basis_per_share is not None else None),
        ordinal=row.ordinal,
        adopted=row.capture_id is not None,
        opened_by=row.opened_by_event_id,
    )


def disposal_fact(row: LotDisposition) -> core_lots.Disposal:
    return core_lots.Disposal(
        event_id=row.event_id,
        lot_id=row.lot_id,
        on=row.disposed_on,
        quantity=Decimal(row.quantity),
        cost_basis=Decimal(row.cost_basis) if row.cost_basis is not None else None,
    )


@dataclass(frozen=True)
class Loaded:
    lots: list[Lot]
    disposals: list[LotDisposition]


def load(session: Session, position_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Loaded]:
    """Every non-voided lot and every disposition (live or not) of these
    positions. The encrypted columns are deferred: no invariant reads them."""
    ids = list(set(position_ids))
    out: dict[uuid.UUID, Loaded] = {}
    if not ids:
        return out
    lots: dict[uuid.UUID, list[Lot]] = defaultdict(list)
    for row in session.execute(
        select(Lot)
        .options(
            load_only(
                Lot.id,
                Lot.position_id,
                Lot.acquired_on,
                Lot.effective_on,
                Lot.quantity,
                Lot.cost_basis,
                Lot.basis_per_share,
                Lot.basis_source,
                Lot.ordinal,
                Lot.capture_id,
                Lot.opened_by_event_id,
                Lot.plan_bidx,
                Lot.external_ref_bidx,
                Lot.acquisition,
                Lot.voided_at,
            )
        )
        .where(Lot.position_id.in_(ids), Lot.voided_at.is_(None))
    ).scalars():
        lots[row.position_id].append(row)
    disposals: dict[uuid.UUID, list[LotDisposition]] = defaultdict(list)
    for d in session.execute(
        select(LotDisposition).where(LotDisposition.position_id.in_(ids))
    ).scalars():
        disposals[d.position_id].append(d)
    for pid in ids:
        if lots.get(pid) or disposals.get(pid):
            out[pid] = Loaded(lots.get(pid, []), disposals.get(pid, []))
    return out


def book(loaded: Loaded | None, events: Sequence[core.Event]) -> core_lots.Book:
    """The live book of one position, given its events: its live lots and
    dispositions, and its effective splits (#624)."""
    effective = core.effective_events(events)
    splits = [
        core_lots.SplitFact(e.occurred_on, e.split[0], e.split[1])
        for e in effective
        if e.kind == core.SPLIT and e.split is not None
    ]
    if loaded is None:
        return core_lots.Book(lots=(), splits=tuple(splits))
    return core_lots.live(
        (lot_fact(r) for r in loaded.lots),
        (disposal_fact(d) for d in loaded.disposals),
        {e.id for e in effective},
        splits,
    )


def books(
    session: Session, events: Mapping[uuid.UUID, Sequence[core.Event]]
) -> dict[uuid.UUID, core_lots.Book]:
    """The live book of each position that has lots (positions without any
    are absent: they keep the running average-cost basis)."""
    loaded = load(session, events.keys())
    out: dict[uuid.UUID, core_lots.Book] = {}
    for pid, found in loaded.items():
        b = book(found, events.get(pid, ()))
        if b.lots or b.disposals:
            out[pid] = b
    return out


def digest(parts: object) -> str:
    """A plain SHA-256 over canonical JSON (state fingerprints)."""
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
