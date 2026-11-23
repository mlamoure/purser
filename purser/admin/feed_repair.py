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
from sqlalchemy.orm import Session, aliased, selectinload

from purser import audit, balances, events, splits, transfers
from purser import ledger as ledger_service
from purser.admin.account_delete import _live_linked_transfers_touching, guarded_delete
from purser.core import ledger as core
from purser.models import (
    Account,
    Connection,
    Match,
    Position,
    PositionEvent,
    Posting,
    RawFetch,
    Transaction,
)

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


def _correction_chains(
    session: Session, doomed: list[uuid.UUID]
) -> dict[uuid.UUID, list[Transaction]]:
    """Every doomed row a correction names (a reversal's ``reverses_id``),
    with the rows of its chain after it — reversals and replacements, in the
    order they were made. One recursive walk forward from all doomed rows.

    Purging such an original alone NULLs its reversal's lineage: the
    replacement stays a live head beside the row the replay re-lands, and
    the account counts that money twice."""
    if not doomed:
        return {}
    base = select(Transaction.id.label("id"), Transaction.reverses_id.label("root")).where(
        Transaction.reverses_id.in_(doomed)
    )
    chain = base.cte("correction_chain", recursive=True)
    later = aliased(Transaction)
    chain = chain.union_all(select(later.id, chain.c.root).where(later.reverses_id == chain.c.id))
    pairs = session.execute(select(chain.c.root, chain.c.id)).all()
    if not pairs:
        return {}
    rows = {
        t.id: t
        for t in session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(Transaction.id.in_({row_id for _root, row_id in pairs}))
        ).scalars()
    }
    chains: dict[uuid.UUID, list[Transaction]] = {}
    for root, row_id in pairs:
        chains.setdefault(root, []).append(rows[row_id])
    for members in chains.values():
        members.sort(key=lambda t: (t.created_at, t.id))
    return chains


def _person_corrected(chains: dict[uuid.UUID, list[Transaction]]) -> list[uuid.UUID]:
    """The doomed rows whose correction a person (or an agent) made: purging
    the chain drops that choice, which the replay cannot bring back."""
    people = {core.Origin.HUMAN.value, core.Origin.AGENT.value}
    return sorted(
        root for root, members in chains.items() if any(t.origin in people for t in members)
    )


def _chains_elsewhere(
    chains: dict[uuid.UUID, list[Transaction]], account_id: uuid.UUID
) -> list[uuid.UUID]:
    """Chains with a leg on another real account (a conversion to a transfer
    in place): deleting them would take that account's leg too."""
    return sorted(
        root
        for root, members in chains.items()
        if any(
            not p.account.is_virtual and p.account_id != account_id
            for t in members
            for p in t.postings
        )
    )


def _sample(ids: list[uuid.UUID]) -> str:
    shown = ", ".join(str(i) for i in ids[:5])
    return shown + (f" (+{len(ids) - 5} more)" if len(ids) > 5 else "")


TRADE_LINK_RECIPE = (
    "to purge it: (1) reopen the position if it is closed (PATCH /positions/<id> "
    "closed=false, or MCP reopen_position); (2) if the account's holdings come from the "
    "feed, switch it to manual (holdings_source=manual); (3) reverse the trade "
    "(POST /position-events/<id>/reverse, or MCP reverse_position_event), which gives the "
    "row its category back; (4) purge with --include-corrections, which takes the row's "
    "whole correction chain; (5) replay the archive (replay-archive); (6) record the trade "
    "again, linking the re-imported row (transaction_id)"
)


def _trade_links(session: Session, doomed: list[uuid.UUID]) -> list[str]:
    """The doomed rows a securities trade linked as its cash (decision 103),
    each described with its trade. Such a trade is undone through the row's
    correction chain, so deleting the row (and NULLing that chain's lineage)
    would leave it impossible to reverse: the purge refuses them until the
    trade is reversed, which frees the row."""
    claimed = set(doomed) & ledger_service.trade_cash_lineage_set(session)
    described: list[str] = []
    for tx_id in sorted(claimed):
        head = session.get(Transaction, tx_id)
        while head is not None:
            later = session.execute(
                select(Transaction).where(Transaction.reverses_id == head.id)
            ).scalar_one_or_none()
            if later is None:
                break
            head = later
        # The trade names the head, or Purser's own cash row that a feed
        # row took over at import (voided into it, #408).
        named = (
            {head.id}
            | set(
                session.execute(
                    select(Transaction.id).where(
                        Transaction.superseded_by == head.id, Transaction.status == "void"
                    )
                ).scalars()
            )
            if head is not None
            else set()
        )
        trade = (
            session.execute(
                select(PositionEvent, Position)
                .join(Position, Position.id == PositionEvent.position_id)
                .where(
                    PositionEvent.transaction_id.in_(named),
                    PositionEvent.reverses_id.is_(None),
                )
            ).first()
            if named
            else None
        )
        if trade is None:
            described.append(f"feed row {tx_id} is a securities trade's cash")
            continue
        event, position = trade
        described.append(
            f"feed row {tx_id} is the cash of the {position.symbol} {event.kind} of "
            f"{format(event.quantity.normalize(), 'f')} on {event.occurred_on.isoformat()} "
            f"(position event {event.id})"
        )
    return described


def purge_preflight(session: Session, account: Account, since: date_type | None) -> dict[str, Any]:
    doomed = _doomed_feed_tx_ids(session, account.id, since)
    chains = _correction_chains(session, doomed)
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
        "trade_links": len(_trade_links(session, doomed)),
        # Corrected feed rows: refused unless --include-corrections, which
        # purges each whole chain (the replay re-lands the bank's row only).
        "corrected": len(chains),
        "correction_rows": sum(len(members) for members in chains.values()),
        "person_corrected": [str(root) for root in _person_corrected(chains)],
        "corrections_elsewhere": [str(root) for root in _chains_elsewhere(chains, account.id)],
    }


def execute_purge(
    session: Session,
    account: Account,
    *,
    since: date_type | None,
    actor_token_id: uuid.UUID | None,
    include_corrections: bool = False,
) -> dict[str, Any]:
    """Delete the account's feed-origin rows in ONE transaction (caller
    commits). Mirrors ``account_delete.execute_delete``'s ordering.

    Refused (``RepairRefused``) while a securities trade holds one of them as
    its cash, and while one was corrected (a reversal names it) unless
    ``include_corrections``: then each corrected row goes with its whole
    correction chain, so nothing of it stands beside the row the replay
    re-lands. A chain with a leg on another real account is refused either
    way."""
    from purser.holdings import projection

    # The account lock a sale takes (lock_position) comes first, so a sale
    # linking one of these rows and this purge exclude each other: the purge
    # sees the committed link (and refuses), or the sale waits and then finds
    # its row gone. Taken before mark_dirty, like every holdings writer.
    account = projection.lock_account(session, account.id)
    doomed = _doomed_feed_tx_ids(session, account.id, since)
    linked = _trade_links(session, doomed)
    if linked:
        raise RepairRefused(f"{account.path}: " + "; ".join(linked) + "; " + TRADE_LINK_RECIPE)
    chains = _correction_chains(session, doomed)
    elsewhere = _chains_elsewhere(chains, account.id)
    if elsewhere:
        raise RepairRefused(
            f"{account.path}: {len(elsewhere)} corrected feed row(s) were converted to a "
            "transfer, so their correction posts to another real account; convert them back "
            f"to a category first: {_sample(elsewhere)}"
        )
    if chains and not include_corrections:
        raise RepairRefused(
            f"{account.path}: {len(chains)} feed row(s) were corrected after import (a "
            "recategorization, a rule, a trade's link); purging the original alone leaves its "
            "correction standing beside the row the replay re-lands, counted twice. Pass "
            "--include-corrections to purge each whole correction chain (a person's "
            f"correction is then lost and must be made again): {_sample(sorted(chains))}"
        )
    chain_ids = [t.id for members in chains.values() for t in members]
    if doomed:
        earliest = session.execute(
            select(func.min(Transaction.date)).where(Transaction.id.in_(doomed + chain_ids))
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
    # A split whose backing rows include doomed rows (decision 118) is removed
    # the same way: its backing rows come back (the clone landing here rejoins
    # the doomed set), so the replay re-lands them beside no split leg.
    doomed_set = set(doomed)
    for t in splits.splits_touching(session, account.id):
        if {b.id for b in splits.current_backers(session, t)} & doomed_set:
            splits.unsplit_transaction(session, tx_id=t.id, token=None)
    session.flush()

    doomed = _doomed_feed_tx_ids(session, account.id, since)  # re-query after unlinking
    chains = _correction_chains(session, doomed)
    corrections = sorted({t.id for members in chains.values() for t in members} - set(doomed))

    # 2-4. Matches, review marks, lineage pointers on survivors (position
    # events included), postings and the transactions themselves — the
    # shared guarded delete.
    deleted = guarded_delete(session, doomed + corrections)
    tx_deleted = deleted["transactions_deleted"]

    summary = {
        "path": account.path,
        "since": since.isoformat() if since else None,
        "transfers_unlinked": transfers_unlinked,
        "corrected_purged": len(chains),
        "correction_rows_purged": len(corrections),
        "person_corrected": [str(root) for root in _person_corrected(chains)],
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
        # Replayed rows may predate an account's horizon (decision 123).
        from purser import starting_balance

        starting_balance.reseat_all(session)
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


def run_purge(
    ref: str, *, since: date_type | None, execute: bool, include_corrections: bool = False
) -> None:
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
        if plan["trade_links"]:
            typer.echo(
                f"  feed rows a securities trade holds as its cash: {plan['trade_links']} "
                "(the purge refuses them; --yes names each trade and the steps)"
            )
        if plan["corrected"]:
            verb = "purged with them" if include_corrections else "refused without"
            typer.echo(
                f"  feed rows corrected after import: {plan['corrected']}, their correction "
                f"chains {plan['correction_rows']} more rows ({verb} --include-corrections)"
            )
            if plan["person_corrected"]:
                typer.echo(
                    "    corrected by a person or an agent (that choice is lost; make it "
                    "again after the replay): " + ", ".join(plan["person_corrected"])
                )
            if plan["corrections_elsewhere"]:
                typer.echo(
                    "    converted to a transfer (refused: convert back to a category "
                    "first): " + ", ".join(plan["corrections_elsewhere"])
                )
        typer.echo("  QIF/manual/agent rows: UNTOUCHED (feed-origin only)")
        typer.echo("  raw_fetches: UNTOUCHED (append-only archive stays whole)")
        if not execute:
            typer.echo("\nDry run — nothing changed. Re-run with --yes to execute.")
            return

        typer.echo(f"\n{_PGDUMP_BANNER}")
        try:
            summary = execute_purge(
                session,
                account,
                since=since,
                actor_token_id=None,
                include_corrections=include_corrections,
            )
        except RepairRefused as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(1) from exc
        session.commit()
        from purser import networth

        networth.consume_watermark()
        typer.echo(
            f"Purged {summary['path']}: {summary['transactions_deleted']} transactions, "
            f"{summary['postings_deleted']} postings, {summary['matches_deleted']} matches "
            f"({summary['transfers_unlinked']} transfers unlinked first; "
            f"{summary['correction_rows_purged']} correction rows of "
            f"{summary['corrected_purged']} corrected feed rows). "
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
