"""Pending-lifecycle reconciliation shared by feed importers (DESIGN §4).

Shape-agnostic: the caller computes the set of content-hashes still present as
pendings in a fetch (however that payload is shaped) and this module voids the
DB pendings that
vanished, linking each to the posted row that replaced it when one exists and
emitting ``pending.vanished`` when the bank simply dropped it. Only pendings
dated on/after ``window_start`` are considered: the fetch doesn't cover anything
older, so absence there proves nothing — which is why a sync reaches its fetch
back to the oldest open pending (``service._open_pendings_start``), so every
pending is re-checked on every sync, not only those inside the resync overlap
(#282, decision 70).

A voided pending can be the bank row of a live match (the matcher pairs
expecteds with pending rows too); ``void_pending`` hands that match to
``matching.settle_voided_pending`` so it follows the posted successor or
releases its bill (#273, decision 69). The importer calls ``void_pending``
too, when the bank posts a pending under the very same identity.

Both paths — successor pairing and the identity move — supersede a pending by
its posted row here and nowhere else, so this is also where a pending's review
mark follows it (``review.carry_to_successor``, decision 82): a row someone
already looked at while it was pending does not come back for review once it
posts, unless the posted row's category differs.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from purser import audit, balances, events
from purser.models import Account, Posting, Transaction

# A vanished pending is considered "became posted" when a posted row for the
# same account and amount lands within this many days of it either way. The
# sync's fetch reaches this far behind the oldest open pending, so a successor
# dated before its pending is fetched too (#282).
PENDING_MATCH_WINDOW_DAYS = 7


def reconcile_vanished_pendings(
    session: Session,
    *,
    account: Account,
    still_pending_hashes: set[str],
    window_start: date,
) -> int:
    """Void this account's import pendings that no longer appear in the fetch.

    ``still_pending_hashes`` is the set of content-hashes the caller still sees
    as pending in the payload; any DB pending (import-origin, dated in-window)
    not in that set is voided — superseded_by a posted replacement when one is
    found, else flagged with a ``pending.vanished`` event."""
    voided = 0
    db_pendings = (
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.status == "pending",
                Transaction.origin == "import",
                Transaction.date >= window_start,
                Posting.account_id == account.id,
            )
            .distinct()
            # Deterministic: when two vanished pendings could claim the same
            # successor, the older one (then the earlier import) gets it.
            .order_by(Transaction.date, Transaction.created_at, Transaction.id)
        )
        .scalars()
        .all()
    )
    for tx in db_pendings:
        if tx.content_hash in still_pending_hashes:
            continue
        void_pending(session, tx, successor=find_posted_replacement(session, tx, account.id))
        voided += 1
    return voided


def void_pending(
    session: Session,
    tx: Transaction,
    *,
    successor: Transaction | None,
    identity_moved: bool = False,
) -> None:
    """Void one import pending the feed reports gone: superseded by its posted
    ``successor`` when there is one, else flagged ``pending.vanished``.
    ``identity_moved`` records that the successor carries the pending's own
    content hash (the bank posted it under the same identity, #282) — the
    importer released the hash from this row before creating the successor.

    A live match on the pending follows it (decision 69): it is captured
    BEFORE the void, so the schedule estimate it feeds can be compared across
    the change, then re-pointed to the successor or released. Its review
    mark follows it to the successor too (decision 82)."""
    # Function-local: the feed layer stays decoupled from the services.
    from purser import matching, review

    live = matching.live_match_on(session, tx)
    tx.status = "void"
    if successor is not None:
        tx.superseded_by = successor.id
    session.flush()
    balances.mark_dirty(session, tx.date)
    audit.record(
        session,
        actor_token_id=None,
        action="transaction.void",
        entity_type="transaction",
        entity_id=tx.id,
        before={"status": "pending"},
        after={
            "status": "void",
            "reason": "pending_posted" if successor else "pending_vanished",
            "superseded_by": str(successor.id) if successor else None,
            **({"identity_moved": True} if identity_moved else {}),
        },
    )
    if successor is not None:
        review.carry_to_successor(session, pending=tx, successor=successor)
    else:
        events.emit(
            session,
            type_="pending.vanished",
            entity_type="transaction",
            entity_id=tx.id,
            payload={"date": tx.date.isoformat(), "description": tx.description},
        )
    if live is not None:
        matching.settle_voided_pending(session, live, successor=successor)


def find_posted_replacement(
    session: Session, pending_tx: Transaction, account_id: uuid.UUID
) -> Transaction | None:
    window = timedelta(days=PENDING_MATCH_WINDOW_DAYS)
    pending_amount = _real_amount(session, pending_tx, account_id)
    if pending_amount is None:
        return None
    superseder = aliased(Transaction)
    candidates = (
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.status == "posted",
                Transaction.origin == "import",
                Transaction.id != pending_tx.id,
                Posting.account_id == account_id,
                Posting.amount == pending_amount,
                Transaction.date >= pending_tx.date - window,
                Transaction.date <= pending_tx.date + window,
                # A posted row already claimed as another FEED pending's
                # successor can't also replace this one. Only feed rows count
                # as claims (#282): a matched expected points superseded_by at
                # the posted row that settled it too, and that claims the
                # bill, not the bank line — it must not hide the successor
                # and void a pending that did post as "vanished".
                ~select(superseder.id)
                .where(
                    superseder.superseded_by == Transaction.id,
                    superseder.source_fetch_id.is_not(None),
                )
                .exists(),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None
    # Deterministic pick: closest date to the pending, then earliest created.
    return min(
        candidates,
        key=lambda tx: (abs((tx.date - pending_tx.date).days), tx.created_at, tx.id),
    )


def _real_amount(session: Session, tx: Transaction, account_id: uuid.UUID) -> Decimal | None:
    row = session.execute(
        select(Posting.amount).where(
            Posting.transaction_id == tx.id, Posting.account_id == account_id
        )
    ).scalar_one_or_none()
    return Decimal(row) if row is not None else None
