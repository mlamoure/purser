"""Guarded purge of retired-origin rows (issue #284, DESIGN §7, decision 72).

Rows a retired ingest path wrote outlive their archive. The Direct-to-Bank
pipeline was removed in 0.3.0 (#97) and migration 0010 purged its captures
(decision 25), so its ledger rows survive with ``source_fetch_id`` NULL.
``purge-feed-rows`` selects rows BY fetch and can never reach them, and no
other path may remove a posted row.

A retired-origin row is an ``origin='import'`` row with no source fetch. That
predicate alone also matches every QIF/CSV row — manual imports keep
``source_fetch_id`` NULL by design (decision 53) — so the unit of removal is a
CREATION BATCH: every row one import committed carries that transaction's
``created_at`` (``now()`` is the transaction timestamp). Listing an account's
batches first, with the change-audit actions recorded at the same instant as
a best-effort provenance hint (``qif.import``, ``connection.ingest_direct``;
pruned after ``audit_retention_days``), is how the operator tells a retired
batch from a manual import before naming it.

For one account and the named batches, in ONE transaction:

- ``expected``/``pending`` rows take the normal void path
  (``ledger.transition_status``): they are freely editable, and the void row
  stays as lineage;
- ``posted`` rows go through the shared guarded delete
  (``account_delete.guarded_delete``), the one the feed purge uses — an
  immutable row has no void path;
- rows already ``void`` are left as lineage.

Linked rows refuse unless ``--unlink``: a live match (proposed -> rejected;
confirmed -> unmatched, so the bill it settled is expected again) and a live
transfer a batch row was merged or absorbed into (unlinked, so the
counterpart's bank line survives on its own account; the batch row's
resurrected clone — found by the content hash that moves to it — goes with
the batch). Rows nothing can unlink always refuse: a corrected row (its
reversal and replacement live outside the batch, and deleting the original
alone would leave the reversal standing on its own), a row that also posts to
another real account, and a merged transfer that was itself corrected.

Dry run by default. The apply run recomputes the plan, refuses when it no
longer equals the preview the operator saw, and rolls back when what it
executed differs from it. ``raw_fetches`` is never touched.
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any

import typer
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from purser import audit, balances, events, matching, transfers
from purser import ledger as ledger_service
from purser.admin.account_delete import guarded_delete
from purser.admin.feed_repair import RepairRefused
from purser.core import ledger as core
from purser.models import Account, AuditLog, Match, Posting, Transaction

# The only origin whose rows ever had a source fetch to lose. Rows of every
# other origin never had one, so a batch of them is not a retired batch.
RETIRED_ORIGIN = "import"
SAMPLE_ROWS = 10

_PGDUMP_BANNER = "⚠  This hard-deletes ledger rows and is IRREVERSIBLE. Run `pg_dump` first.\n"


def resolve_account(session: Session, ref: str) -> Account:
    account = ledger_service.resolve_account(session, ref)  # LedgerError -> caller
    if account.is_virtual:
        raise RepairRefused(f"{account.path} is a category, not a real account")
    if account.closed_at is not None:
        raise RepairRefused(f"{account.path} is closed; reopen it before purging retired rows")
    return account


def parse_batch(value: str) -> datetime:
    """A batch is named by its ``created_at`` exactly as the listing prints it
    (ISO 8601 with its UTC offset); a naive timestamp would be ambiguous."""
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RepairRefused(f"{value!r} is not an ISO 8601 timestamp") from exc
    if stamp.tzinfo is None:
        raise RepairRefused(
            f"{value!r} has no UTC offset; copy the batch timestamp exactly as listed"
        )
    return stamp


def _retired_ids(account_id: uuid.UUID, batches: Sequence[datetime] | None) -> Select[Any]:
    """Ids of the account's import rows with no source fetch, any status."""
    stmt = (
        select(Transaction.id)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account_id,
            Transaction.origin == RETIRED_ORIGIN,
            Transaction.source_fetch_id.is_(None),
        )
        .distinct()
    )
    if batches is not None:
        stmt = stmt.where(Transaction.created_at.in_(batches))
    return stmt


# --------------------------------------------------------------------------
# Listing: the account's creation batches
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Batch:
    created_at: datetime
    status_counts: dict[str, int]
    first_date: date_type
    last_date: date_type
    provenance: tuple[str, ...]  # change-audit actions at the same instant

    @property
    def rows(self) -> int:
        return sum(self.status_counts.values())


def list_batches(session: Session, account: Account) -> list[Batch]:
    grouped = session.execute(
        select(
            Transaction.created_at,
            Transaction.status,
            func.count(),
            func.min(Transaction.date),
            func.max(Transaction.date),
        )
        .where(Transaction.id.in_(_retired_ids(account.id, None)))
        .group_by(Transaction.created_at, Transaction.status)
        .order_by(Transaction.created_at)
    ).all()
    statuses: dict[datetime, dict[str, int]] = defaultdict(dict)
    spans: dict[datetime, tuple[date_type, date_type]] = {}
    for created_at, status, count, first, last in grouped:
        statuses[created_at][status] = int(count)
        lo, hi = spans.get(created_at, (first, last))
        spans[created_at] = (min(lo, first), max(hi, last))
    provenance: dict[datetime, set[str]] = defaultdict(set)
    if statuses:
        # Every audit row of a transaction carries its timestamp too (both
        # columns default to now()), so the import's own change rows sit at
        # the batch's instant. Per-row transaction.* rows are noise here.
        for at, action in session.execute(
            select(AuditLog.at, AuditLog.action)
            .where(
                AuditLog.at.in_(list(statuses)),
                AuditLog.kind == audit.KIND_CHANGE,
                AuditLog.action.not_like("transaction.%"),
            )
            .distinct()
        ):
            provenance[at].add(action)
    return [
        Batch(
            created_at=created_at,
            status_counts=counts,
            first_date=spans[created_at][0],
            last_date=spans[created_at][1],
            provenance=tuple(sorted(provenance.get(created_at, set()))),
        )
        for created_at, counts in statuses.items()
    ]


# --------------------------------------------------------------------------
# Plan: what the purge would do — computed identically by dry run and apply
# --------------------------------------------------------------------------


@dataclass
class RetiredPlan:
    account: Account
    batches: tuple[datetime, ...]
    rows: list[Transaction]
    to_void: list[Transaction] = field(default_factory=list)  # expected/pending
    to_delete: list[Transaction] = field(default_factory=list)  # posted
    left_void: list[Transaction] = field(default_factory=list)  # already void
    matches_reject: list[Match] = field(default_factory=list)  # live, proposed
    matches_unmatch: list[Match] = field(default_factory=list)  # live, confirmed
    transfers: list[Transaction] = field(default_factory=list)  # live, to unlink
    held: list[Transaction] = field(default_factory=list)  # void batch rows they hold
    resurrected_void: int = 0  # held rows unlink brings back pending
    resurrected_delete: int = 0  # ... and posted
    empty_batches: list[datetime] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # refused even with --unlink

    @property
    def links(self) -> int:
        return len(self.matches_reject) + len(self.matches_unmatch) + len(self.transfers)

    def counts(self) -> dict[str, int]:
        """The numbers an apply run must reproduce exactly."""
        return {
            "rows": len(self.rows),
            "transactions_voided": len(self.to_void) + self.resurrected_void,
            "transactions_deleted": len(self.to_delete) + self.resurrected_delete,
            "left_void": len(self.left_void),
            "matches_rejected": len(self.matches_reject),
            "matches_unmatched": len(self.matches_unmatch),
            "transfers_unlinked": len(self.transfers),
        }

    def refusal(self, *, unlink: bool) -> str | None:
        if not self.batches:
            return "name at least one batch to purge"
        if self.empty_batches:
            missing = ", ".join(b.isoformat() for b in self.empty_batches)
            return f"no retired-origin rows of batch {missing} on {self.account.path}"
        if self.blocked:
            return "; ".join(self.blocked)
        if self.links and not unlink:
            return (
                f"{self.links} link(s) hold rows of this batch (live matches or merged "
                "transfers); re-run with --unlink to unlink them first"
            )
        return None


def _real_postings_elsewhere(tx: Transaction, account_id: uuid.UUID) -> bool:
    return any(not p.account.is_virtual and p.account_id != account_id for p in tx.postings)


def _id_sample(rows: Sequence[Transaction]) -> str:
    shown = ", ".join(str(r.id) for r in rows[:5])
    return shown + (f" (+{len(rows) - 5} more)" if len(rows) > 5 else "")


def plan_purge(session: Session, account: Account, batches: Sequence[datetime]) -> RetiredPlan:
    ids = list(session.execute(_retired_ids(account.id, batches)).scalars())
    rows = (
        list(
            session.execute(
                select(Transaction)
                .options(selectinload(Transaction.postings).selectinload(Posting.account))
                .where(Transaction.id.in_(ids))
                .order_by(Transaction.date, Transaction.id)
            ).scalars()
        )
        if ids
        else []
    )
    plan = RetiredPlan(account=account, batches=tuple(batches), rows=rows)
    present = {r.created_at for r in rows}
    plan.empty_batches = [b for b in batches if b not in present]
    for row in rows:
        if row.status == core.Status.POSTED:
            plan.to_delete.append(row)
        elif row.status == core.Status.VOID:
            plan.left_void.append(row)
        else:  # expected (an uncleared manual row) or pending
            plan.to_void.append(row)
    live = plan.to_void + plan.to_delete

    # Refused whatever the flags: nothing can unlink these.
    elsewhere = [r for r in live if _real_postings_elsewhere(r, account.id)]
    if elsewhere:
        plan.blocked.append(
            f"{len(elsewhere)} row(s) also post to another real account (a transfer); "
            f"convert them back to a category first: {_id_sample(elsewhere)}"
        )
    row_ids = [r.id for r in rows]
    corrected_ids = (
        set(
            session.execute(
                select(Transaction.reverses_id).where(Transaction.reverses_id.in_(row_ids))
            ).scalars()
        )
        if row_ids
        else set()
    )
    corrected = [r for r in rows if r.id in corrected_ids]
    if corrected:
        plan.blocked.append(
            f"{len(corrected)} row(s) were corrected after import; their reversal and "
            "replacement live outside the batch, and removing the original alone would "
            f"leave the reversal standing on its own: {_id_sample(corrected)}"
        )

    # Live transfers holding void batch rows (merged or absorbed): unlinking
    # resurrects each held row on this account, and that clone goes too.
    held_by: dict[uuid.UUID, list[Transaction]] = defaultdict(list)
    for row in plan.left_void:
        if row.superseded_by is not None:
            held_by[row.superseded_by].append(row)
    if held_by:
        holders = session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(
                Transaction.id.in_(list(held_by)),
                Transaction.status.in_(matching.MATCHABLE_STATUSES),
                Transaction.superseded_by.is_(None),
            )
            .order_by(Transaction.date, Transaction.id)
        ).scalars()
        for holder in holders:
            if not transfers.is_transfer(holder):
                continue  # a pending's posted twin, a matched bank row: no link to undo
            held = held_by[holder.id]
            corrected_holder = session.execute(
                select(Transaction.id).where(Transaction.reverses_id == holder.id).limit(1)
            ).scalar_one_or_none()
            if corrected_holder is not None:
                plan.blocked.append(
                    f"transfer {holder.id} holds rows of this batch but was itself "
                    "corrected; it cannot be unlinked"
                )
                continue
            if any(r.content_hash is None for r in held):
                plan.blocked.append(
                    f"transfer {holder.id} holds a batch row without a content hash, so "
                    "its resurrection could not be traced; resolve it by hand"
                )
                continue
            plan.transfers.append(holder)
            plan.held.extend(held)
            # unlink_transfer brings each original back with its pre-merge
            # status, falling back to the transfer's own.
            prior = transfers._prior_statuses_for(session, holder.id)
            for row in held:
                if prior.get(str(row.id), holder.status) == core.Status.POSTED:
                    plan.resurrected_delete += 1
                else:
                    plan.resurrected_void += 1

    # Live matches on live batch rows or on the transfers to unlink.
    linked_ids = [r.id for r in live] + [t.id for t in plan.transfers]
    if linked_ids:
        for match in session.execute(
            select(Match)
            .where(
                Match.state.in_(["proposed", "confirmed"]),
                or_(Match.expected_id.in_(linked_ids), Match.posted_id.in_(linked_ids)),
            )
            .order_by(Match.created_at, Match.id)
        ).scalars():
            if match.state == "proposed":
                plan.matches_reject.append(match)
            elif match.posted_id in linked_ids:
                plan.matches_unmatch.append(match)
            else:  # pragma: no cover - a confirmed match's expected side is void
                plan.blocked.append(f"match {match.id} has an unexpected shape")
    return plan


# --------------------------------------------------------------------------
# Execute
# --------------------------------------------------------------------------


def execute_purge(
    session: Session,
    account: Account,
    batches: Sequence[datetime],
    *,
    unlink: bool,
    actor_token_id: uuid.UUID | None,
    previewed: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Purge the named batches in ONE transaction (caller commits).

    Raises ``RepairRefused`` before writing anything when the plan refuses or
    no longer matches the ``previewed`` counts the operator saw, and after
    writing (the caller rolls back) when what ran differs from the plan."""
    plan = plan_purge(session, account, batches)
    refusal = plan.refusal(unlink=unlink)
    if refusal is not None:
        raise RepairRefused(refusal)
    expected = plan.counts()
    if previewed is not None and previewed != expected:
        raise RepairRefused(
            f"the batch changed since the preview ({previewed} -> {expected}); run it again"
        )

    # 1. Matches: proposals rejected; a confirmed match is undone, so the
    # bill the doomed row settled is expected again.
    for match in plan.matches_reject:
        matching.reject_match(session, match_id=match.id, token=None)
    for match in plan.matches_unmatch:
        matching.unmatch(session, match_id=match.id, token=None)

    # 2. Transfers holding batch rows: the counterpart's bank line comes back
    # on its own account; each held row's clone (its content hash moves with
    # it, decision 29) joins the purge.
    moved_hashes = [r.content_hash for r in plan.held if r.content_hash is not None]
    for holder in plan.transfers:
        transfers.unlink_transfer(session, transfer_id=holder.id, token=None)
    session.flush()
    clones = (
        list(
            session.execute(
                select(Transaction).where(
                    Transaction.content_hash.in_(moved_hashes),
                    Transaction.status.in_(matching.MATCHABLE_STATUSES),
                )
            ).scalars()
        )
        if moved_hashes
        else []
    )

    # 3. Expected/pending rows: the normal void path.
    void_ids = [r.id for r in plan.to_void] + [
        c.id for c in clones if c.status != core.Status.POSTED
    ]
    for tx_id in void_ids:
        ledger_service.transition_status(
            session, tx_id=tx_id, new_status=core.Status.VOID, token=None
        )

    # 4. Posted rows: the shared guarded delete.
    delete_ids = [r.id for r in plan.to_delete] + [
        c.id for c in clones if c.status == core.Status.POSTED
    ]
    if delete_ids:
        earliest = session.execute(
            select(func.min(Transaction.date)).where(Transaction.id.in_(delete_ids))
        ).scalar_one()
        balances.mark_dirty(session, earliest)
    deleted = guarded_delete(session, delete_ids)

    executed = {
        "rows": len(plan.rows),
        "transactions_voided": len(void_ids),
        "transactions_deleted": deleted["transactions_deleted"],
        "left_void": len(plan.left_void),
        "matches_rejected": len(plan.matches_reject),
        "matches_unmatched": len(plan.matches_unmatch),
        "transfers_unlinked": len(plan.transfers),
    }
    if executed != expected:
        raise RepairRefused(
            f"the purge did {executed}, not the planned {expected}; nothing was committed"
        )

    summary: dict[str, Any] = {
        "path": account.path,
        "batches": [b.isoformat() for b in batches],
        **executed,
        "postings_deleted": deleted["postings_deleted"],
        "matches_deleted": deleted["matches_deleted"],
        "reviews_deleted": deleted["reviews_deleted"],
        "lineage_nulled": deleted["lineage_nulled"],
        "voided_ids": [str(i) for i in void_ids],
        "deleted_ids": [str(i) for i in delete_ids],
    }
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.purge_retired_rows",
        entity_type="account",
        entity_id=account.id,
        before=summary,
    )
    events.emit(
        session,
        type_="account.retired_rows_purged",
        entity_type="account",
        entity_id=account.id,
        payload={
            "path": account.path,
            "batches": summary["batches"],
            "transactions_voided": executed["transactions_voided"],
            "transactions_deleted": executed["transactions_deleted"],
        },
    )
    session.flush()
    return summary


# --------------------------------------------------------------------------
# CLI orchestration (thin shell around the service functions above)
# --------------------------------------------------------------------------


def _status_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{s}={n}" for s, n in sorted(counts.items()))


def _print_batches(account: Account, batches: list[Batch]) -> None:
    if not batches:
        typer.echo(f"No import rows without a source fetch on {account.path}; nothing to purge.")
        return
    typer.echo(f"Account: {account.path}")
    typer.echo("Import rows with no source fetch, by creation batch (oldest first):")
    for b in batches:
        audit_hint = ", ".join(b.provenance) if b.provenance else "none retained"
        typer.echo(
            f"  {b.created_at.isoformat()}  rows={b.rows} ({_status_text(b.status_counts)})"
            f"  dated {b.first_date.isoformat()}..{b.last_date.isoformat()}  audit: {audit_hint}"
        )
    typer.echo(
        "\nPreview one with --batch <created_at> (dry run), then add --apply to execute."
        " Manual QIF/CSV imports are batches too — check the audit column before naming one."
    )


def _row_line(row: Transaction, account_id: uuid.UUID) -> str:
    amount = sum(
        (Decimal(p.amount) for p in row.postings if p.account_id == account_id), Decimal("0")
    )
    others = [p.account for p in row.postings if p.account_id != account_id]
    where = ", ".join(a.path if a.is_virtual else f"[{a.path}]" for a in others) or "-"
    description = (row.description or "")[:40]
    return f"    {row.date.isoformat()}  {row.status:<8} {amount:>12}  {where}  {description}"


def _print_plan(plan: RetiredPlan, *, unlink: bool) -> None:
    counts = plan.counts()
    typer.echo(f"Account: {plan.account.path}")
    typer.echo(f"Batches: {', '.join(b.isoformat() for b in plan.batches)}")
    statuses = Counter(r.status for r in plan.rows)
    typer.echo(
        f"  retired-origin rows in these batches: {counts['rows']} ({_status_text(statuses)})"
    )
    typer.echo(f"  pending/expected rows to void (normal void path): {len(plan.to_void)}")
    typer.echo(f"  posted rows to hard-delete (guarded delete): {len(plan.to_delete)}")
    typer.echo(f"  void rows left as lineage: {counts['left_void']}")
    links = (
        f"  links: {len(plan.matches_reject) + len(plan.matches_unmatch)} live match(es), "
        f"{len(plan.transfers)} merged transfer(s)"
    )
    typer.echo(links + ("  -> unlinked first (--unlink)" if plan.links and unlink else ""))
    if plan.links:
        typer.echo(
            f"    matches rejected={counts['matches_rejected']}, "
            f"unmatched={counts['matches_unmatched']} (each settled bill is expected again)"
        )
        typer.echo(
            f"    transfers unlinked={counts['transfers_unlinked']}: the counterpart's bank "
            f"line survives on its own account; this account's side comes back and goes "
            f"with the batch (void={plan.resurrected_void}, delete={plan.resurrected_delete})"
        )
    typer.echo(
        f"  total: voided={counts['transactions_voided']}, deleted={counts['transactions_deleted']}"
    )
    if plan.rows:
        typer.echo(f"  sample ({min(SAMPLE_ROWS, len(plan.rows))} of {len(plan.rows)}):")
        for row in plan.rows[:SAMPLE_ROWS]:
            typer.echo(_row_line(row, plan.account.id))
    typer.echo("  rows outside these batches: UNTOUCHED")
    typer.echo("  raw_fetches: UNTOUCHED (append-only archive stays whole)")


def run_purge_retired(ref: str, *, batch_values: list[str], unlink: bool, execute: bool) -> None:
    """Entry point for `purser admin purge-retired-rows`."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        try:
            account = resolve_account(session, ref)
            batches = [parse_batch(v) for v in batch_values]
        except ledger_service.LedgerError as exc:  # not found, or a malformed path (#214)
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except RepairRefused as exc:
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(1) from exc

        if not batches:
            if execute:
                typer.echo(
                    "refused: name the batch(es) to purge with --batch "
                    "(run without --apply to list them)",
                    err=True,
                )
                raise typer.Exit(1)
            _print_batches(account, list_batches(session, account))
            return

        plan = plan_purge(session, account, batches)
        _print_plan(plan, unlink=unlink)
        refusal = plan.refusal(unlink=unlink)
        if refusal is not None:
            typer.echo(f"\nrefused: {refusal}", err=True)
            raise typer.Exit(1)
        if not execute:
            typer.echo("\nDry run — nothing changed. Re-run with --apply to execute.")
            return

        typer.echo(f"\n{_PGDUMP_BANNER}")
        try:
            summary = execute_purge(
                session,
                account,
                batches,
                unlink=unlink,
                actor_token_id=None,
                previewed=plan.counts(),
            )
        except RepairRefused as exc:
            session.rollback()
            typer.echo(f"refused: {exc}", err=True)
            raise typer.Exit(1) from exc
        session.commit()
        from purser import networth

        networth.consume_watermark()
        typer.echo(
            f"Purged {summary['path']}: {summary['transactions_voided']} voided, "
            f"{summary['transactions_deleted']} deleted "
            f"({summary['transfers_unlinked']} transfers unlinked, "
            f"{summary['matches_rejected'] + summary['matches_unmatched']} matches undone first)."
        )
