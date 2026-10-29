"""Guarded feed-row purge + archive replay (issue #172, decision 45).

Remediation pair for feed data that imported wrong (e.g. a sign-inverted
stream, issue #170) and needs re-deriving from the archive:

- ``purge-feed-rows``: delete an account's FEED-ORIGIN ledger rows
  (``origin='import' AND source_fetch_id IS NOT NULL`` — never QIF, manual,
  agent, or system rows), any status, so their content hashes stop
  squatting identity. Dry-run by default; ``--yes`` executes in ONE
  transaction, mirroring ``account_delete``'s guarded pattern.
- ``replay-archive``: re-import a connection's archived payloads oldest
  first via ``importer.import_fetch(raw, window_start=None, replay=True)``
  — §7's "ledger is a projection of the archive" made operational.
  Content-hash idempotent, so it is safe to run repeatedly; with the
  account's ``feed_invert`` now set, purged rows re-land with corrected
  signs and their ORIGINAL hashes (hash-on-raw, decision 43). Ledger rows
  only: balance valuations and holdings snapshots describe the moment each
  fetch was made, which the live sync recorded, so a replay writes neither
  (#328).

``raw_fetches`` is NEVER touched — the archive stays whole and re-derivable.
"""

import uuid
from datetime import date as date_type
from typing import Any

import typer
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from purser import audit, balances, events, transfers
from purser import ledger as ledger_service
from purser.admin.account_delete import _live_linked_transfers_touching, guarded_delete
from purser.models import Account, Connection, Match, Posting, RawFetch, Transaction

_PGDUMP_BANNER = "⚠  This hard-deletes ledger rows and is IRREVERSIBLE. Run `pg_dump` first.\n"


class RepairRefused(Exception):
    """A structural precondition rejects the repair (-> exit 1)."""


def resolve_purge_account(session: Session, ref: str) -> Account:
    account = ledger_service.resolve_account(session, ref)  # LedgerError -> caller
    if account.is_virtual:
        raise RepairRefused(f"{account.path} is a category, not a real account")
    if account.closed_at is not None:
        raise RepairRefused(f"{account.path} is closed; reopen it before purging feed rows")
    return account


def _doomed_feed_tx_ids(
    session: Session, account_id: uuid.UUID, since: date_type | None
) -> list[uuid.UUID]:
    """Feed-origin transactions with a posting on the account — ANY status
    (voided merge originals still squat their content hashes, and replay
    must be able to re-land every row)."""
    stmt = (
        select(Posting.transaction_id)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == account_id,
            Transaction.origin == "import",
            Transaction.source_fetch_id.is_not(None),
        )
        .distinct()
    )
    if since is not None:
        stmt = stmt.where(Transaction.date >= since)
    return list(session.execute(stmt).scalars())


def _merge_transfers_touching_doomed(
    session: Session, account: Account, doomed: list[uuid.UUID]
) -> list[Transaction]:
    """Live merge-created transfers whose linked originals include a doomed
    row — they must unlink first so the counterpart's bank line survives on
    its own account."""
    if not doomed:
        return []
    doomed_set = set(doomed)
    result = []
    for t in _live_linked_transfers_touching(session, account.id):
        child_ids = set(
            session.execute(
                select(Transaction.id).where(
                    Transaction.superseded_by == t.id, Transaction.status == "void"
                )
            ).scalars()
        )
        if child_ids & doomed_set:
            result.append(t)
    return result


def purge_preflight(session: Session, account: Account, since: date_type | None) -> dict[str, Any]:
    doomed = _doomed_feed_tx_ids(session, account.id, since)
    status_counts: dict[str, int] = {}
    matches = 0
    if doomed:
        status_counts = dict(
            session.execute(
                select(Transaction.status, func.count())
                .where(Transaction.id.in_(doomed))
                .group_by(Transaction.status)
            ).all()
        )
        matches = session.execute(
            select(func.count())
            .select_from(Match)
            .where(or_(Match.expected_id.in_(doomed), Match.posted_id.in_(doomed)))
        ).scalar_one()
    return {
        "path": account.path,
        "since": since.isoformat() if since else None,
        "doomed_count": len(doomed),
        "status_counts": status_counts,
        "merge_transfers": len(_merge_transfers_touching_doomed(session, account, doomed)),
        "matches": matches,
    }


def execute_purge(
    session: Session,
    account: Account,
    *,
    since: date_type | None,
    actor_token_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Delete the account's feed-origin rows in ONE transaction (caller
    commits). Mirrors ``account_delete.execute_delete``'s ordering."""
    doomed = _doomed_feed_tx_ids(session, account.id, since)
    if doomed:
        earliest = session.execute(
            select(func.min(Transaction.date)).where(Transaction.id.in_(doomed))
        ).scalar_one()
        if earliest is not None:
            balances.mark_dirty(session, earliest)

    # 1. Unlink live merge-transfers whose originals include doomed rows: the
    # counterpart's bank row resurrects on its surviving account carrying its
    # content_hash; the feed-origin clone landing here rejoins the doomed set.
    transfers_unlinked = 0
    for t in _merge_transfers_touching_doomed(session, account, doomed):
        transfers.unlink_transfer(session, transfer_id=t.id, token=None)
        transfers_unlinked += 1
    session.flush()

    doomed = _doomed_feed_tx_ids(session, account.id, since)  # re-query after unlinking

    # 2-4. Matches, review marks, lineage pointers on survivors, postings and
    # the transactions themselves — the shared guarded delete.
    deleted = guarded_delete(session, doomed)
    tx_deleted = deleted["transactions_deleted"]

    summary = {
        "path": account.path,
        "since": since.isoformat() if since else None,
        "transfers_unlinked": transfers_unlinked,
        **deleted,
    }
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.purge_feed_rows",
        entity_type="account",
        entity_id=account.id,
        before=summary,
    )
    events.emit(
        session,
        type_="account.feed_rows_purged",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path, "transactions_deleted": tx_deleted},
    )
    session.flush()
    return summary


def replay_plan(
    session: Session, connection_ref: str, since: date_type | None
) -> tuple[Connection, list[uuid.UUID]]:
    try:
        connection_id = uuid.UUID(connection_ref)
    except ValueError as exc:
        raise RepairRefused(f"{connection_ref!r} is not a connection UUID") from exc
    connection = session.get(Connection, connection_id)
    if connection is None:
        raise RepairRefused(f"connection {connection_ref} not found")
    stmt = (
        select(RawFetch.id)
        .where(RawFetch.connection_id == connection.id)
        .order_by(RawFetch.fetched_at.asc(), RawFetch.id.asc())
    )
    if since is not None:
        stmt = stmt.where(func.date(RawFetch.fetched_at) >= since)
    return connection, list(session.execute(stmt).scalars())


def execute_replay(
    session: Session, fetch_ids: list[uuid.UUID], *, actor_token_id: uuid.UUID | None
) -> dict[str, int]:
    """Re-import archived payloads oldest-first, committing per fetch so an
    interrupted replay keeps its progress (idempotent — a re-run skips).
    ``replay=True``: ledger rows only, no valuations or holdings (#328)."""
    from purser.feed import importer

    totals: dict[str, int] = {}
    for fetch_id in fetch_ids:
        raw = session.get(RawFetch, fetch_id)
        if raw is None:  # pragma: no cover - ids come from the plan query
            continue
        stats = importer.import_fetch(session, raw, window_start=None, replay=True)
        for key, value in stats.items():
            if isinstance(value, int):  # counts; the re-described rows list is not one
                totals[key] = totals.get(key, 0) + value
        session.commit()
    totals["fetches_replayed"] = len(fetch_ids)
    if fetch_ids:
        connection_id = session.execute(
            select(RawFetch.connection_id).where(RawFetch.id == fetch_ids[0])
        ).scalar_one()
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="connection.replay_archive",
            entity_type="connection",
            entity_id=connection_id,
            after=totals,
        )
        session.commit()
    return totals


# --------------------------------------------------------------------------
# CLI orchestration (thin shells around the service functions above)
# --------------------------------------------------------------------------


def run_purge(ref: str, *, since: date_type | None, execute: bool) -> None:
    """Entry point for `purser admin purge-feed-rows`."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        try:
            account = resolve_purge_account(session, ref)
        except ledger_service.LedgerError as exc:  # not found, or a malformed path (#214)
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except RepairRefused as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(1) from exc

        plan = purge_preflight(session, account, since)
        typer.echo(f"Account: {plan['path']}  (since: {plan['since'] or 'all history'})")
        typer.echo(f"  feed-origin transactions to delete: {plan['doomed_count']}")
        if plan["status_counts"]:
            by_status = ", ".join(f"{s}={n}" for s, n in sorted(plan["status_counts"].items()))
            typer.echo(f"    by status: {by_status}")
        typer.echo(f"  merge-transfers to unlink first: {plan['merge_transfers']}")
        typer.echo(f"  matches to delete: {plan['matches']}")
        typer.echo("  QIF/manual/agent rows: UNTOUCHED (feed-origin only)")
        typer.echo("  raw_fetches: UNTOUCHED (append-only archive stays whole)")
        if not execute:
            typer.echo("\nDry run — nothing changed. Re-run with --yes to execute.")
            return

        typer.echo(f"\n{_PGDUMP_BANNER}")
        summary = execute_purge(session, account, since=since, actor_token_id=None)
        session.commit()
        from purser import networth

        networth.consume_watermark()
        typer.echo(
            f"Purged {summary['path']}: {summary['transactions_deleted']} transactions, "
            f"{summary['postings_deleted']} postings, {summary['matches_deleted']} matches "
            f"({summary['transfers_unlinked']} transfers unlinked first). "
            "Replay the archive to re-derive them."
        )


def run_replay(connection_ref: str, *, since: date_type | None, execute: bool) -> None:
    """Entry point for `purser admin replay-archive`."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        try:
            connection, fetch_ids = replay_plan(session, connection_ref, since)
        except RepairRefused as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(1) from exc

        typer.echo(
            f"Connection {connection.id}: {len(fetch_ids)} archived fetches to replay"
            f" (since: {since.isoformat() if since else 'all history'})"
        )
        if not execute:
            typer.echo("\nDry run — nothing changed. Re-run with --yes to execute.")
            return

        totals = execute_replay(session, fetch_ids, actor_token_id=None)
        from purser import networth

        networth.consume_watermark()
        summary_keys = (
            "imported",
            "skipped",
            "skipped_zero_amount",
            "skipped_before_feed_start",
            "skipped_masked_duplicate",
            "skipped_redescribed",
        )
        rendered = ", ".join(f"{k}={totals.get(k, 0)}" for k in summary_keys)
        typer.echo(f"Replayed {totals['fetches_replayed']} fetches: {rendered}")
