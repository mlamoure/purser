"""Guarded hard-delete of a duplicate account (DESIGN §3.4, decision 33).

This is a remediation tool, never an API endpoint: hard delete FREES the
``qif:``-namespaced content hashes of the account's imported rows (voiding
would squat them forever — decision 29 hash semantics), so a mis-imported QIF
can be re-imported onto the right account. It is dry-run by default and prints
a pre-flight report; ``--yes`` executes inside ONE DB transaction.

``raw_fetches`` is NEVER touched — the archive stays whole and re-derivable.

Refuses (exit 1) on: account not found, virtual (category), SimpleFIN-attached
(``connection_id`` set — close unmaps, so close first), or an
account with child accounts.
"""

import uuid
from typing import Any

import typer
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session, selectinload

from purser import audit, balances, events, locks, matching, transfers
from purser import ledger as ledger_service
from purser.models import (
    Account,
    AccountReconciliation,
    HoldingsSnapshot,
    Match,
    Position,
    PositionEvent,
    Posting,
    Schedule,
    Suggestion,
    Transaction,
    TransactionReview,
    Valuation,
)

# Triggers guarding posted immutability; disabled only inside the delete
# transaction (steps 3-4) and re-enabled before commit (they are catalog
# changes, rolled back on any error).
_POSTED_GUARD_TRIGGERS = (
    ("transactions", "transactions_posted_immutable"),
    ("transactions", "transactions_posted_undeletable"),
    ("postings", "postings_posted_immutable"),
)


class DeleteRefused(Exception):
    """A structural precondition rejects the delete (-> exit 1)."""


def resolve_and_check(session: Session, ref: str) -> Account:
    """Resolve the account and enforce the refusal preconditions."""
    account = ledger_service.resolve_account(session, ref)  # LedgerError -> caller
    if account.is_virtual:
        raise DeleteRefused(f"{account.path} is a category, not a real account")
    # Feed attachment = a live connection mapping. A bare external_id with no
    # connection is import provenance (QIF-created accounts carry the file's
    # account id) and must not block deletion — those are the exact accounts
    # this tool exists to remove.
    if account.connection_id is not None:
        raise DeleteRefused(
            f"{account.path} is attached to a SimpleFIN feed; close it first "
            "(closing unmaps) or unmap it before deleting"
        )
    child = session.execute(
        select(Account.id).where(Account.parent_id == account.id).limit(1)
    ).scalar_one_or_none()
    if child is not None:
        raise DeleteRefused(f"{account.path} has child accounts; delete or re-parent them first")
    return account


def _doomed_tx_ids(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
    """Every transaction with >=1 posting on the account — ANY status, void
    lineage included (a duplicate account's whole footprint goes)."""
    return list(
        session.execute(
            select(Posting.transaction_id).where(Posting.account_id == account_id).distinct()
        ).scalars()
    )


def _live_linked_transfers_touching(session: Session, account_id: uuid.UUID) -> list[Transaction]:
    """Live merge/absorb-created transfers with a leg on the account — the rows
    whose voided originals must be resurrected (so the counterpart's bank line
    survives on its own account) before the account's footprint is deleted.
    Split loan payments included (decision 62): their counterpart bank row
    must survive just the same."""
    rows = session.execute(
        select(Transaction)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Transaction.status.in_(matching.MATCHABLE_STATUSES),
            Transaction.superseded_by.is_(None),
            Posting.account_id == account_id,
        )
        .distinct()
    ).scalars()
    return [
        t
        for t in rows
        if transfers.is_transfer(t) and transfers.has_linked_originals(session, t.id)
    ]


def preflight(session: Session, account: Account) -> dict[str, Any]:
    """Read-only plan: what the execute step WOULD touch. The doomed set here
    is pre-unlink; unlinking resurrects a clone on the account that joins it."""
    doomed = _doomed_tx_ids(session, account.id)
    status_counts: dict[str, int] = {}
    matches = lineage = 0
    if doomed:
        status_counts = {
            status: count
            for status, count in session.execute(
                select(Transaction.status, func.count())
                .where(Transaction.id.in_(doomed))
                .group_by(Transaction.status)
            )
        }
        matches = session.execute(
            select(func.count())
            .select_from(Match)
            .where(or_(Match.expected_id.in_(doomed), Match.posted_id.in_(doomed)))
        ).scalar_one()
        lineage = session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                or_(Transaction.superseded_by.in_(doomed), Transaction.reverses_id.in_(doomed)),
                Transaction.id.not_in(doomed),
            )
        ).scalar_one()
    return {
        "path": account.path,
        "doomed_count": len(doomed),
        "status_counts": status_counts,
        "merge_transfers": len(_live_linked_transfers_touching(session, account.id)),
        "matches": matches,
        "lineage_pointers": lineage,
        "schedules": session.execute(
            select(func.count()).select_from(Schedule).where(Schedule.account_id == account.id)
        ).scalar_one(),
        "valuations": session.execute(
            select(func.count()).select_from(Valuation).where(Valuation.account_id == account.id)
        ).scalar_one(),
        "holdings": session.execute(
            select(func.count())
            .select_from(HoldingsSnapshot)
            .where(HoldingsSnapshot.account_id == account.id)
        ).scalar_one(),
        "reconciliations": session.execute(
            select(func.count())
            .select_from(AccountReconciliation)
            .where(AccountReconciliation.account_id == account.id)
        ).scalar_one(),
        "positions": session.execute(
            select(func.count()).select_from(Position).where(Position.account_id == account.id)
        ).scalar_one(),
        "position_events": session.execute(
            select(func.count())
            .select_from(PositionEvent)
            .join(Position, Position.id == PositionEvent.position_id)
            .where(Position.account_id == account.id)
        ).scalar_one(),
        "suggestions": session.execute(
            select(func.count()).select_from(Suggestion).where(_names_account(account.id))
        ).scalar_one(),
    }


def _names_account(account_id: uuid.UUID) -> Any:
    """Suggestions naming the account as subject or counterparty: their foreign
    keys carry no ON DELETE, so the account row cannot go while any remains
    (#399)."""
    return or_(
        Suggestion.subject_account_id == account_id,
        Suggestion.counterparty_account_id == account_id,
    )


def _set_triggers(session: Session, enabled: bool) -> None:
    verb = "ENABLE" if enabled else "DISABLE"
    for table, trigger in _POSTED_GUARD_TRIGGERS:
        session.execute(text(f"ALTER TABLE {table} {verb} TRIGGER {trigger}"))


def _rowcount(result: Any) -> int:
    """DML rowcount. Session.execute() is typed ``Result`` (no rowcount);
    DELETE/UPDATE return a CursorResult at runtime — one typed seam here."""
    return int(result.rowcount or 0)


def guarded_delete(session: Session, doomed: list[uuid.UUID]) -> dict[str, int]:
    """Hard-delete the ``doomed`` transactions and everything that points at
    them, inside the caller's transaction (the caller marks the balance
    watermark, audits and commits). The one guarded delete every admin
    remediation shares — delete-account, purge-feed-rows, purge-retired-rows.

    Order matters:

    1. match rows referencing a doomed transaction (either side);
    2. review marks — an annotation about a row goes with the row. Migration
       0021 backfilled a mark onto every row that existed then, so without
       this step the foreign key refuses the delete outright (#284);
    3. with the posted-immutable guards disabled: lineage pointers
       (``superseded_by`` / ``reverses_id``) on SURVIVING transactions NULLed
       (the 0010-migration precedent), then postings, then the transactions.

    Running balances follow the transactions by FK cascade.
    """
    counts = {
        "matches_deleted": 0,
        "reviews_deleted": 0,
        "lineage_nulled": 0,
        "postings_deleted": 0,
        "transactions_deleted": 0,
    }
    if not doomed:
        return counts
    counts["matches_deleted"] = _rowcount(
        session.execute(
            delete(Match).where(or_(Match.expected_id.in_(doomed), Match.posted_id.in_(doomed)))
        )
    )
    counts["reviews_deleted"] = _rowcount(
        session.execute(
            delete(TransactionReview).where(TransactionReview.transaction_id.in_(doomed))
        )
    )
    # Disable the posted-immutable guards for the lineage NULLs (posted
    # survivors) and the deletes, re-enabling before commit. Postgres refuses
    # ALTER TABLE while deferred constraint events are pending, so flush them
    # first (SET CONSTRAINTS ALL IMMEDIATE checks the — balanced — ledger now);
    # restore DEFERRED after so later balanced inserts in this same session
    # still work.
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    _set_triggers(session, enabled=False)
    try:
        for column in (Transaction.superseded_by, Transaction.reverses_id):
            counts["lineage_nulled"] += _rowcount(
                session.execute(
                    update(Transaction)
                    .where(column.in_(doomed), Transaction.id.not_in(doomed))
                    .values({column.key: None})
                )
            )
        # Postings first (FK), then the transactions themselves.
        counts["postings_deleted"] = _rowcount(
            session.execute(delete(Posting).where(Posting.transaction_id.in_(doomed)))
        )
        counts["transactions_deleted"] = _rowcount(
            session.execute(delete(Transaction).where(Transaction.id.in_(doomed)))
        )
    finally:
        _set_triggers(session, enabled=True)
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    return counts


def execute_delete(
    session: Session, account: Account, *, actor_token_id: uuid.UUID | None
) -> dict[str, Any]:
    """Delete the account's whole footprint in ONE transaction (caller
    commits). Order matters — see the numbered steps below."""
    # Account before watermark (#403): lock the row before mark_dirty below,
    # the order every account writer keeps.
    locks.lock_account(session, account.id)
    # Capture the dirty horizon BEFORE anything is unlinked or deleted: the
    # earliest transaction/valuation/holdings date this account touches
    # (decision 38). Balance rows follow the doomed transactions via FK
    # CASCADE; the commit hook recomputes every other account from here.
    horizon_candidates = [
        session.execute(
            select(func.min(Transaction.date))
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(Posting.account_id == account.id)
        ).scalar_one_or_none(),
        session.execute(
            select(func.min(Valuation.as_of)).where(Valuation.account_id == account.id)
        ).scalar_one_or_none(),
        session.execute(
            select(func.min(HoldingsSnapshot.as_of)).where(
                HoldingsSnapshot.account_id == account.id
            )
        ).scalar_one_or_none(),
    ]
    horizon = [d for d in horizon_candidates if d is not None]
    if horizon:
        balances.mark_dirty(session, min(horizon))

    was_closed = account.closed_at is not None
    if was_closed:
        # Reopen for the duration: unlink resurrects clones on this account, and
        # the closed-account guards would otherwise refuse those inserts. The
        # account row is deleted at the end, so this leaves no trace.
        account.closed_at = None
        session.flush()

    # 1. Unlink live merge-created transfers touching the account: the
    # counterpart's bank rows resurrect on their surviving accounts carrying
    # their content_hash; the clone landing here joins the doomed set.
    transfers_unlinked = 0
    for t in _live_linked_transfers_touching(session, account.id):
        transfers.unlink_transfer(session, transfer_id=t.id, token=None)
        transfers_unlinked += 1
    session.flush()

    doomed = _doomed_tx_ids(session, account.id)  # re-query after unlinking

    # Reconciliations reference their adjustment transaction (one of the
    # doomed rows) by FK, so they go before the account's transactions do.
    reconciliations_deleted = _rowcount(
        session.execute(
            delete(AccountReconciliation).where(AccountReconciliation.account_id == account.id)
        )
    )

    # 2-4. Matches, review marks, lineage pointers on survivors, postings and
    # the transactions themselves — the shared guarded delete.
    deleted = guarded_delete(session, doomed)
    if deleted["lineage_nulled"]:
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="transaction.relink",
            entity_type="account",
            entity_id=account.id,
            after={
                "nulled_lineage_pointers": deleted["lineage_nulled"],
                "reason": "account.delete",
            },
        )

    # 5. Schedules targeting the account; valuations / holdings snapshots for it;
    # its positions and their events.
    schedules_deleted = _rowcount(
        session.execute(delete(Schedule).where(Schedule.account_id == account.id))
    )
    valuations_deleted = _rowcount(
        session.execute(delete(Valuation).where(Valuation.account_id == account.id))
    )
    holdings_deleted = _rowcount(
        session.execute(delete(HoldingsSnapshot).where(HoldingsSnapshot.account_id == account.id))
    )
    # Positions tracked by hand (#390): their manual snapshots went just above
    # (they reference the positions); then the events, then the positions.
    # security_prices is global and stays.
    position_ids = select(Position.id).where(Position.account_id == account.id)
    position_events_deleted = _rowcount(
        session.execute(delete(PositionEvent).where(PositionEvent.position_id.in_(position_ids)))
    )
    positions_deleted = _rowcount(
        session.execute(delete(Position).where(Position.account_id == account.id))
    )
    # Inbox rows naming the account, open or decided: derived state, like its
    # schedules, and their FKs would otherwise refuse the account row (#399).
    suggestions_deleted = _rowcount(
        session.execute(delete(Suggestion).where(_names_account(account.id)))
    )

    # 6. Audit + event, then the account row. raw_fetches is untouched.
    summary = {
        "path": account.path,
        "name": account.name,
        "was_closed": was_closed,
        "transfers_unlinked": transfers_unlinked,
        **deleted,
        "schedules_deleted": schedules_deleted,
        "valuations_deleted": valuations_deleted,
        "holdings_deleted": holdings_deleted,
        "reconciliations_deleted": reconciliations_deleted,
        "positions_deleted": positions_deleted,
        "position_events_deleted": position_events_deleted,
        "suggestions_deleted": suggestions_deleted,
    }
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.delete",
        entity_type="account",
        entity_id=account.id,
        before=summary,
    )
    events.emit(
        session,
        type_="account.deleted",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path},
    )
    session.delete(account)
    session.flush()
    return summary


# --------------------------------------------------------------------------
# CLI orchestration (thin shell around the service functions above)
# --------------------------------------------------------------------------

_PGDUMP_BANNER = "⚠  This hard-deletes ledger rows and is IRREVERSIBLE. Run `pg_dump` first.\n"


def _print_report(plan: dict[str, Any], *, execute: bool) -> None:
    typer.echo(f"Account: {plan['path']}")
    typer.echo(f"  transactions to delete: {plan['doomed_count']}")
    if plan["status_counts"]:
        by_status = ", ".join(f"{s}={n}" for s, n in sorted(plan["status_counts"].items()))
        typer.echo(f"    by status: {by_status}")
    typer.echo(f"  merge-transfers to unlink first: {plan['merge_transfers']}")
    typer.echo(f"  matches to delete: {plan['matches']}")
    typer.echo(f"  lineage pointers to NULL on survivors: {plan['lineage_pointers']}")
    typer.echo(f"  schedules to delete: {plan['schedules']}")
    typer.echo(f"  valuations to delete: {plan['valuations']}")
    typer.echo(f"  holdings snapshots to delete: {plan['holdings']}")
    typer.echo(f"  reconciliations to delete: {plan['reconciliations']}")
    typer.echo(
        f"  positions to delete: {plan['positions']} "
        f"({plan['position_events']} position events); security prices stay"
    )
    typer.echo(f"  suggestions to delete: {plan['suggestions']}")
    typer.echo("  raw_fetches: UNTOUCHED (append-only archive stays whole)")
    typer.echo(
        "  NOTE: re-importing the freed QIF hashes into another account needs a "
        "date cutoff to avoid duplicating any SimpleFIN rows that overlap the "
        "same dates."
    )


def run_delete(ref: str, *, execute: bool) -> None:
    """Entry point for `purser admin delete-account`."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        try:
            account = resolve_and_check(session, ref)
        except ledger_service.LedgerError as exc:  # not found, or a malformed path (#214)
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except DeleteRefused as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(1) from exc

        plan = preflight(session, account)
        _print_report(plan, execute=execute)
        if not execute:
            typer.echo("\nDry run — nothing changed. Re-run with --yes to execute.")
            return

        typer.echo(f"\n{_PGDUMP_BANNER}")
        summary = execute_delete(session, account, actor_token_id=None)
        session.commit()
        # The commit hook already recomputed running balances; net worth is
        # the async consumer — run it inline so the CLI leaves nothing stale.
        from purser import networth

        networth.consume_watermark()
        typer.echo(
            f"Deleted {summary['path']}: {summary['transactions_deleted']} transactions, "
            f"{summary['postings_deleted']} postings, {summary['matches_deleted']} matches, "
            f"{summary['schedules_deleted']} schedules "
            f"({summary['transfers_unlinked']} transfers unlinked first)."
        )
