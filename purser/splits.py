"""Editable split transactions — category and ``[Account]`` lines, the Quicken
model (issue #590, DESIGN §3.3, decision 118; amends 87, generalizes 62).

A split is the row being split — its real leg keeps the bank's exact amount —
plus two or more lines. Each line is a category or another open real account
(``[Account]``); the lines sum to the row's amount in the flat form's sign, and
each line books its negation: to the category, or as a leg on that account.
Zero-sum by construction (``core.expand_split``). The row carries the split
marker (``transactions.split_account_id`` = its own account), so a split is
never a transfer, whatever its postings look like.

An ``[Account]`` line may be **backed** by that account's own bank rows: they
are merged into the split with lineage, exactly as a confirmed transfer merges
its originals (void, ``superseded_by`` → the split), so the money is counted
once; several rows may back one line (a 401(k) feed that sends one row per
fund). An unbacked leg is Purser's own. Un-split resurrects every backing row
as a clone carrying its prior status and its content hash, as unlink does.

Writes are explicit (write scope, audited ``transaction.split`` /
``transaction.unsplit``, evented). A posted row is split, re-split and
un-split through reversal + replacement (the content hash stays on the
corrected original, so re-import adds nothing); a manual pending row in place.
A bank pending is refused until it posts: the bank's posted row replaces it.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased, selectinload

from purser import audit, balances, events, matching
from purser import ledger as ledger_service
from purser import lineage as bank_lineage
from purser import review as review_service
from purser import rules as rules_service
from purser import suggestions as suggestions_service
from purser.admin import security
from purser.core import ledger as core
from purser.feed.reconcile import PENDING_MATCH_WINDOW_DAYS
from purser.ledger import LedgerError
from purser.ledger.accounts import _assert_accounts_open
from purser.models import Account, ApiToken, Event, Posting, Suggestion, Transaction

# Backing candidates for an [Account] line are listed this many days either
# side of the split row's date — the window the feed's own pending→posted
# pairing uses (``feed.reconcile``), so a contribution the payroll and the
# 401(k) feed date a few days apart is found.
BACKING_WINDOW_DAYS = PENDING_MATCH_WINDOW_DAYS
CANDIDATES_MAX = 20
# One request's bounds, for every surface that splits (REST, MCP, a split
# suggestion's edited lines; #648 L2): each line costs account lookups and
# each backing row a lock, so a request stays well inside what one call
# should hold.
MAX_LINES = 50
MAX_MEMO_CHARS = 500
MAX_BACKING_ROWS = 50  # named across all of a split's lines

EVENT_SPLIT = "transaction.split"
EVENT_UNSPLIT = "transaction.unsplit"
EVENT_BACKING_POSTED = "transaction.split_backing_posted"


class SplitError(LedgerError):
    """A split request the ledger refuses (a 422)."""


@dataclass(frozen=True)
class LineInput:
    """One requested line: ``target`` is a category path or id, or an
    ``[account path or id]``; ``amount`` is the line's share of the row's
    amount in the flat form's sign; ``backing_ids`` name bank rows on an
    ``[Account]`` line's account that back its leg."""

    target: str
    amount: Decimal
    memo: str | None = None
    backing_ids: tuple[uuid.UUID, ...] = ()


def check_limits(lines: list[LineInput]) -> None:
    """Refuse a request past :data:`MAX_LINES` lines, a memo past
    :data:`MAX_MEMO_CHARS` characters or more than :data:`MAX_BACKING_ROWS`
    backing rows, before anything is read or locked."""
    if len(lines) > MAX_LINES:
        raise SplitError(f"a split takes at most {MAX_LINES} lines, not {len(lines)}")
    for i, line in enumerate(lines, start=1):
        if line.memo is not None and len(line.memo) > MAX_MEMO_CHARS:
            raise SplitError(f"line {i}: a memo is at most {MAX_MEMO_CHARS} characters")
    backing = sum(len(line.backing_ids) for line in lines)
    if backing > MAX_BACKING_ROWS:
        raise SplitError(
            f"a split names at most {MAX_BACKING_ROWS} backing rows across its lines, not {backing}"
        )


@dataclass
class _Line:
    account: Account
    is_account: bool
    amount: Decimal
    memo: str | None
    backers: list[Transaction] = field(default_factory=list)

    @property
    def leg(self) -> Decimal:
        """The posting this line books: the negation of its share."""
        return -self.amount

    @property
    def target(self) -> str:
        return f"[{self.account.path}]" if self.is_account else str(self.account.path)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _load(session: Session, tx_id: uuid.UUID) -> Transaction:
    return matching._load_tx(session, tx_id)


def is_split(tx: Transaction) -> bool:
    return tx.split_account_id is not None


def own_leg(tx: Transaction) -> Posting | None:
    """The split account's leg (a split), or the only real leg of a row with
    exactly one real and one category leg (a flat row); None otherwise."""
    if tx.split_account_id is not None:
        return next(
            (
                p
                for p in tx.postings
                if p.account_id == tx.split_account_id and not p.account.is_virtual
            ),
            None,
        )
    real = [p for p in tx.postings if not p.account.is_virtual]
    virtual = [p for p in tx.postings if p.account.is_virtual]
    if len(real) == 1 and len(virtual) == 1:
        return real[0]
    return None


def current_backers(session: Session, tx: Transaction) -> list[Transaction]:
    """The bank rows merged into split ``tx`` (void, ``superseded_by`` → it),
    never an expected it settles (``transfers.linked_original_clause``)."""
    from purser import transfers  # function-local: transfers imports matching

    if tx.split_account_id is None:
        return []
    return list(
        session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.superseded_by == tx.id,
                Transaction.status == "void",
                transfers.linked_original_clause(),
            )
            .order_by(Transaction.date, Transaction.created_at, Transaction.id)
        ).scalars()
    )


def _split_events(session: Session, head_id: uuid.UUID) -> list[Event]:
    """The split's events in the order they were written: ``seq`` orders
    the events one database transaction wrote (they share ``at``)."""
    found = session.execute(
        select(Event).where(
            Event.entity_id == head_id, Event.type.in_([EVENT_SPLIT, EVENT_BACKING_POSTED])
        )
    ).scalars()
    return sorted(found, key=lambda e: (e.at, int((e.payload or {}).get("seq", 0))))


def _next_seq(session: Session, head_id: uuid.UUID) -> int:
    return len(_split_events(session, head_id)) + 1


def _prior_statuses(session: Session, head_id: uuid.UUID) -> dict[str, str]:
    """backing row id -> its status before it was merged, from the split's
    events: the ``transaction.split`` that merged it, then any
    ``transaction.split_backing_posted`` that handed a pending's place to its
    posted successor."""
    prior: dict[str, str] = {}
    for event in _split_events(session, head_id):
        payload = event.payload or {}
        if event.type == EVENT_SPLIT:
            for line in payload.get("lines", []):
                for backer in line.get("backing", []):
                    prior[backer["id"]] = backer["status"]
        else:
            prior.pop(payload.get("from_id", ""), None)
            prior[payload["to_id"]] = payload.get("to_status", "posted")
    return prior


def _event_lines(session: Session, head_id: uuid.UUID) -> list[dict[str, Any]]:
    """The lines of the latest ``transaction.split`` on ``head_id`` (their
    order and backing ids), with backing handed on by later events."""
    lines: list[dict[str, Any]] = []
    for event in _split_events(session, head_id):
        payload = event.payload or {}
        if event.type == EVENT_SPLIT:
            lines = [dict(line) for line in payload.get("lines", [])]
            continue
        for line in lines:
            backing = [
                {**b, "id": payload["to_id"]} if b["id"] == payload.get("from_id") else b
                for b in line.get("backing", [])
            ]
            line["backing"] = backing
    return lines


def describe_lines(session: Session, tx: Transaction) -> list[dict[str, Any]]:
    """The lines of split ``tx``, in the order they were written: ``target``
    (a category path, or ``[account path]``), ``kind`` (``category`` |
    ``account``), ``amount`` (the flat form's sign, a Decimal), ``memo``, and
    for an account line ``backing_ids`` (the bank rows merged into it). A
    split never written through this module (a QIF import) lists its lines
    largest first."""
    own = own_leg(tx)
    if tx.split_account_id is None or own is None:
        return []
    legs = [p for p in tx.postings if p is not own]
    backers = current_backers(session, tx)
    backer_account = {
        b.id: next(p.account_id for p in b.postings if not p.account.is_virtual) for b in backers
    }
    recorded = _event_lines(session, tx.id)
    ordered: list[tuple[Posting, list[uuid.UUID]]] = []
    remaining = list(legs)
    for recorded_line in recorded:
        match = next(
            (
                p
                for p in remaining
                if (f"[{p.account.path}]" if not p.account.is_virtual else str(p.account.path))
                == recorded_line.get("target")
                and core.quantize(-Decimal(p.amount))
                == core.quantize(Decimal(recorded_line["amount"]))
            ),
            None,
        )
        if match is None:
            continue
        remaining.remove(match)
        ids = [uuid.UUID(b["id"]) for b in recorded_line.get("backing", [])]
        ordered.append((match, [i for i in ids if i in backer_account]))
    remaining.sort(key=lambda p: (-abs(Decimal(p.amount)), str(p.account.path)))
    ordered.extend((p, []) for p in remaining)
    # Backers no recorded line names go to the first line on their account.
    claimed = {i for _, ids in ordered for i in ids}
    for backer_id, account_id in backer_account.items():
        if backer_id in claimed:
            continue
        for posting, ids in ordered:
            if posting.account_id == account_id and not posting.account.is_virtual:
                ids.append(backer_id)
                break
    out: list[dict[str, Any]] = []
    for posting, ids in ordered:
        is_account = not posting.account.is_virtual
        line: dict[str, Any] = {
            "target": f"[{posting.account.path}]" if is_account else str(posting.account.path),
            "kind": "account" if is_account else "category",
            "amount": core.quantize(-Decimal(posting.amount)),
            "memo": posting.memo,
        }
        if is_account:
            line["backing_ids"] = ids
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_write(token: ApiToken | None, what: str) -> None:
    if token is not None and not security.can_write(token.scopes):
        raise SplitError(f"{what} requires write scope")


def _refuse_row(session: Session, tx: Transaction) -> None:
    """The rows a split never takes, each refusal naming what to do."""
    from purser import transfers  # function-local: transfers imports matching

    if tx.status == core.Status.VOID.value:
        raise SplitError("void transactions are immutable lineage")
    if tx.status == core.Status.EXPECTED.value:
        raise SplitError(
            "an expected transaction is not split: split the row once it is pending or posted"
        )
    if tx.status == core.Status.PENDING.value and tx.origin == core.Origin.IMPORT.value:
        raise SplitError(
            "this is the bank's pending row; split it once it posts (the bank's posted row "
            "replaces the pending one)"
        )
    if tx.status == core.Status.POSTED.value:
        ledger_service.refuse_corrected(session, tx)
    if transfers.is_transfer(tx):
        raise SplitError(
            "a transfer is not split: unlink it, or convert it back to a category, first"
        )
    if ledger_service.has_system_leg(tx.postings):
        raise SplitError(
            "this row moves with a securities trade or an asset's value; it cannot be split"
        )
    _assert_accounts_open(session, {p.account_id for p in tx.postings})


def _resolve_target(session: Session, target: str, own: Posting) -> tuple[Account, bool]:
    ref = core.parse_transfer_target(target)
    if ref is not None:
        account = ledger_service.resolve_account(session, ref)
        if account.is_virtual:
            raise SplitError(f"{account.path} is a category; write it without brackets")
        if account.id == own.account_id:
            raise SplitError(
                f"a split line cannot be the split's own account ({account.path}); "
                "lines name a category or another account"
            )
        if account.closed_at is not None:
            raise SplitError(f"{account.path} is closed; reopen it to book a split line to it")
        if account.currency.strip() != own.currency.strip():
            raise SplitError("currency mismatch (v1 has no FX)")
        return account, True
    account = ledger_service.resolve_account(session, target)
    if not account.is_virtual:
        raise SplitError(
            f"{account.path} is a real account; write it as [{account.path}] for an account line"
        )
    ledger_service.refuse_system_account(account)
    ledger_service.check_category_currency(own.currency, account)
    return account, False


def _load_rows(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, Transaction]:
    if not ids:
        return {}
    return {
        row.id: row
        for row in session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(Transaction.id.in_(ids))
        ).scalars()
    }


def _check_backer(session: Session, row: Transaction, line: _Line, split: Transaction) -> None:
    """A bank row may back an [Account] line when it is a live one-sided row
    on that account that carries a bank line (an import row, or one whose
    lineage starts at one), is neither a transfer nor a split nor trade cash,
    and no live match holds it."""
    where = f"row {row.id}"
    if row.id == split.id:
        raise SplitError(f"{where} is the row being split")
    if row.status not in matching.MATCHABLE_STATUSES or row.superseded_by is not None:
        raise SplitError(f"{where} is {row.status}: only a live pending or posted row backs a line")
    ledger_service.refuse_corrected(session, row)
    if row.split_account_id is not None:
        raise SplitError(f"{where} is a split; a backing row is a single bank row")
    real = [p for p in row.postings if not p.account.is_virtual]
    virtual = [p for p in row.postings if p.account.is_virtual]
    if len(real) != 1 or len(virtual) != 1:
        raise SplitError(f"{where} is not a single bank row (one account leg, one category)")
    if real[0].account_id != line.account.id:
        raise SplitError(f"{where} is on {real[0].account.path}, not on {line.account.path}")
    if ledger_service.has_system_leg(row.postings):
        raise SplitError(f"{where} moves with a securities trade or an asset's value")
    if row.id not in matching.bank_line_ids(session, [row]):
        raise SplitError(
            f"{where} is not a bank row: a backing row is one the bank (or an import) delivered"
        )
    if matching.live_match_on(session, row) is not None:
        raise SplitError(f"{where} settles a scheduled transaction; unmatch it first")


@dataclass
class _Plan:
    tx: Transaction
    own: Posting
    lines: list[_Line]
    specs: list[core.PostingSpec]
    existing: list[Transaction]
    added: list[Transaction]
    kept: list[Transaction]
    dropped: list[Transaction]


def _plan(session: Session, tx: Transaction, requested: list[LineInput]) -> _Plan:
    _refuse_row(session, tx)
    own = own_leg(tx)
    if own is None:
        raise SplitError(
            "only a row with one account leg and one category, or a split, can be split"
        )
    if len(requested) < 2:
        raise SplitError("a split needs at least two lines; one line is a flat row")
    existing = current_backers(session, tx)
    existing_ids = {b.id for b in existing}
    lines: list[_Line] = []
    asked: list[list[uuid.UUID]] = []
    for item in requested:
        account, is_account = _resolve_target(session, item.target, own)
        memo = item.memo.strip() if item.memo and item.memo.strip() else None
        if item.backing_ids and not is_account:
            raise SplitError(f"{account.path} is a category line; only an [Account] line is backed")
        lines.append(_Line(account, is_account, core.quantize(item.amount), memo))
        asked.append(list(item.backing_ids))
    try:
        specs = core.expand_split(
            account_id=own.account_id,
            amount=Decimal(own.amount),
            lines=[
                core.SplitLine(line.account.id, line.amount, line.memo, line.is_account)
                for line in lines
            ],
            currency=own.currency.strip(),
        )
    except ValueError as exc:
        raise SplitError(f"not a split: {exc}") from exc
    # Follow stale ids along the bank line (a correction since they were
    # read moved it onto a replacement); a row backing this split stays.
    flat_ids = [i for ids in asked for i in ids]
    if len(set(flat_ids)) != len(flat_ids):
        raise SplitError("a backing row can back one line only")
    fresh = [i for i in flat_ids if i not in existing_ids]
    current = bank_lineage.current_rows(session, fresh) if fresh else {}
    resolved = [[i if i in existing_ids else current[i] for i in ids] for ids in asked]
    rows = _load_rows(session, [i for ids in resolved for i in ids])
    missing = [str(i) for ids in resolved for i in ids if i not in rows]
    if missing:
        raise SplitError(f"backing row not found: {', '.join(missing)}")
    added = [rows[i] for ids in resolved for i in ids if i not in existing_ids]
    # Rows first (``ledger.lock_rows``): a concurrent correction or merge of
    # a backing row waits for this split, or this split for it.
    ledger_service.lock_rows(session, [tx, *existing, *added])
    for line, ids in zip(lines, resolved, strict=True):
        line.backers = [rows[i] for i in ids]
        for row in line.backers:
            if row.id in existing_ids:
                if row.postings and not any(
                    p.account_id == line.account.id and not p.account.is_virtual
                    for p in row.postings
                ):
                    raise SplitError(f"row {row.id} is not on {line.account.path}")
                continue
            _check_backer(session, row, line, tx)
        if line.backers:
            total = sum(
                (
                    Decimal(next(p.amount for p in b.postings if not p.account.is_virtual))
                    for b in line.backers
                ),
                Decimal(0),
            )
            if core.quantize(total) != line.leg:
                raise SplitError(
                    f"the rows backing {line.target} sum to {core.quantize(total)}; the line "
                    f"books {line.leg} on that account (its amount {line.amount}, negated)"
                )
    requested_ids = {b.id for line in lines for b in line.backers}
    return _Plan(
        tx=tx,
        own=own,
        lines=lines,
        specs=specs,
        existing=existing,
        added=added,
        kept=[b for b in existing if b.id in requested_ids],
        dropped=[b for b in existing if b.id not in requested_ids],
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _rewrite_in_place(
    session: Session,
    *,
    tx: Transaction,
    specs: list[core.PostingSpec],
    split_account_id: uuid.UUID | None,
    token: ApiToken | None,
    action: str,
    audit_extra: dict[str, Any],
) -> Transaction:
    """A manual pending row takes its new postings in place (DESIGN §4: a
    pending row is freely editable), audited like a correction."""
    before = ledger_service._tx_audit_shape(tx, ledger_service._specs_of(tx))
    for posting in list(tx.postings):
        session.delete(posting)
    session.flush()
    for spec in specs:
        session.add(
            Posting(
                transaction_id=tx.id,
                account_id=spec.account_id,
                amount=spec.amount,
                currency=spec.currency,
                memo=spec.memo,
            )
        )
    tx.split_account_id = split_account_id
    session.flush()
    session.expire(tx, ["postings"])
    balances.mark_dirty(session, tx.date)
    review_service.mark_if_in_scope(session, tx=tx, token=token, via=review_service.VIA_UPDATE)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action=action,
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after={**ledger_service._tx_audit_shape(tx, specs), **audit_extra},
    )
    return tx


def _write(
    session: Session,
    *,
    tx: Transaction,
    specs: list[core.PostingSpec],
    split_account_id: uuid.UUID | None,
    token: ApiToken | None,
    action: str,
    audit_extra: dict[str, Any],
    keep_review_mark: bool = False,
) -> Transaction:
    """A posted row through reversal + replacement, a manual pending in
    place. ``keep_review_mark`` (un-split): the replacement keeps the row's
    review mark instead of a new one stamped now; in place, a row keeps its
    mark anyway (``review.mark`` only adds)."""
    if tx.status == core.Status.POSTED.value:
        return ledger_service.reverse_and_replace_with_specs(
            session,
            tx=tx,
            token=token,
            replacement_specs=specs,
            action=action,
            audit_extra=audit_extra,
            split_account_id=split_account_id,
            keep_review_mark=keep_review_mark,
        )
    return _rewrite_in_place(
        session,
        tx=tx,
        specs=specs,
        split_account_id=split_account_id,
        token=token,
        action=action,
        audit_extra=audit_extra,
    )


def _merge_backer(session: Session, row: Transaction, head: Transaction) -> None:
    """Void one bank row into the split: the void-with-lineage the posted
    guard allows (status and ``superseded_by`` in one UPDATE)."""
    row.status = core.Status.VOID.value
    row.superseded_by = head.id
    session.flush()
    balances.mark_dirty(session, row.date)


def _restore(
    session: Session,
    rows: list[Transaction],
    prior: dict[str, str],
    token: ApiToken | None,
) -> list[Transaction]:
    from purser import transfers  # function-local: transfers imports matching

    clones = []
    for row in rows:
        status = core.Status(prior.get(str(row.id), core.Status.POSTED.value))
        clone, _ = transfers.resurrect_original(session, row, status=status, token=token)
        balances.mark_dirty(session, row.date)
        clones.append(clone)
    return clones


def _close_split_cards(
    session: Session,
    row_ids: list[uuid.UUID],
    *,
    token: ApiToken | None,
    reason: str,
    via_suggestion: uuid.UUID | None,
) -> None:
    roots = bank_lineage.correction_roots(session, row_ids)
    fingerprints = {suggestions_service.split_fingerprint(root) for root in roots.values()}
    rows = session.execute(
        select(Suggestion).where(
            Suggestion.kind == suggestions_service.KIND_SPLIT,
            Suggestion.status == suggestions_service.STATUS_OPEN,
            Suggestion.fingerprint.in_(fingerprints),
        )
    ).scalars()
    for card in list(rows):
        if card.id == via_suggestion:
            continue
        suggestions_service._close_card(
            session,
            card,
            status=suggestions_service.STATUS_WITHDRAWN,
            actor_token_id=token.id if token else None,
            reason=reason,
        )


def _withdraw_pair_cards(session: Session, rows: list[Transaction], token: ApiToken | None) -> None:
    """A row merged into a split pairs with nothing any more: withdraw open
    transfer_pair cards naming it (the sweep after the next import would)."""
    if not rows:
        return
    roots = bank_lineage.correction_roots(session, [r.id for r in rows])
    for root in set(roots.values()):
        cards = session.execute(
            select(Suggestion).where(
                Suggestion.kind == suggestions_service.KIND_TRANSFER_PAIR,
                Suggestion.status == suggestions_service.STATUS_OPEN,
                Suggestion.fingerprint.contains(str(root), autoescape=True),
            )
        ).scalars()
        for card in list(cards):
            suggestions_service._close_card(
                session,
                card,
                status=suggestions_service.STATUS_WITHDRAWN,
                actor_token_id=token.id if token else None,
                reason="transaction.split",
            )


def _plan_out(plan: _Plan) -> dict[str, Any]:
    return {
        "account": str(plan.own.account.path),
        "amount": core.quantize(Decimal(plan.own.amount)),
        "lines": [
            {
                "target": line.target,
                "kind": "account" if line.is_account else "category",
                "amount": line.amount,
                "memo": line.memo,
                **({"backing_ids": [str(b.id) for b in line.backers]} if line.is_account else {}),
            }
            for line in plan.lines
        ],
        "backing_added": [str(b.id) for b in plan.added],
        "backing_restored": [str(b.id) for b in plan.dropped],
    }


def split_transaction(
    session: Session,
    *,
    tx_id: uuid.UUID,
    lines: list[LineInput],
    token: ApiToken | None,
    dry_run: bool = False,
    via_suggestion: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Create or replace the split of a row (DESIGN §3.3, decision 118).

    The row keeps its real leg at the bank's exact amount; ``lines`` (two or
    more) replace whatever category or lines it had. Backing rows named on
    an ``[Account]`` line are merged into the split; rows that backed it
    before and are not named again come back exactly as they were. A posted
    row becomes a reversal + replacement (the split), a manual pending row
    is rewritten in place. ``dry_run`` validates everything and writes
    nothing. ``token=None`` is a system actor."""
    _require_write(token, "splitting a transaction")
    if token is None:
        raise SplitError("a split is written by a person or an agent, never automatically")
    check_limits(lines)
    tx = _load(session, tx_id)
    plan = _plan(session, tx, lines)
    out = _plan_out(plan)
    if dry_run:
        return {"dry_run": True, "transaction_id": str(tx.id), **out}
    was_split = tx.split_account_id is not None
    prior = _prior_statuses(session, tx.id)
    for row in plan.added:
        prior[str(row.id)] = row.status
    event_lines = [
        {
            "target": line.target,
            "amount": str(line.amount),
            "backing": [{"id": str(b.id), "status": prior[str(b.id)]} for b in line.backers],
        }
        for line in plan.lines
    ]
    head = _write(
        session,
        tx=tx,
        specs=plan.specs,
        split_account_id=plan.own.account_id,
        token=token,
        action="transaction.split",
        audit_extra={
            "split_lines": event_lines,
            "backing_added": out["backing_added"],
            "backing_restored": out["backing_restored"],
            "replaced_split": was_split,
        },
    )
    restored = _restore(session, plan.dropped, prior, token)
    for row in plan.kept:
        if row.superseded_by != head.id:
            row.superseded_by = head.id  # a void row: lineage moves to the new head
    session.flush()
    for row in plan.added:
        _merge_backer(session, row, head)
    _withdraw_pair_cards(session, plan.added, token)
    _close_split_cards(
        session, [tx.id], token=token, reason="transaction.split", via_suggestion=via_suggestion
    )
    events.emit(
        session,
        type_=EVENT_SPLIT,
        entity_type="transaction",
        entity_id=head.id,
        payload={
            "seq": _next_seq(session, head.id),
            "transaction_id": str(head.id),
            "replaces_id": str(tx.id) if head.id != tx.id else None,
            "account": out["account"],
            "amount": str(out["amount"]),
            "lines": event_lines,
            "restored_ids": [str(c.id) for c in restored],
        },
    )
    session.expire(head, ["postings"])
    return {
        "transaction_id": str(head.id),
        "replaces_id": str(tx.id) if head.id != tx.id else None,
        "status": head.status,
        **out,
        "restored_ids": [str(c.id) for c in restored],
    }


def unsplit_transaction(
    session: Session,
    *,
    tx_id: uuid.UUID,
    token: ApiToken | None,
    category_ref: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove a split (decision 118): the row goes back to one real leg and
    one category — ``category_ref``, else the uncategorized placeholder for
    its sign — keeping its review mark (decision 130), and every backing row
    comes back exactly as it was (a clone carrying its prior status, content
    hash and review mark, as unlink restores a merged original).
    ``token=None`` is a system actor (the account purge)."""
    _require_write(token, "removing a split")
    tx = _load(session, tx_id)
    if tx.split_account_id is None:
        raise SplitError("this row is not a split")
    if tx.status not in matching.MATCHABLE_STATUSES:
        raise SplitError(f"transaction is {tx.status}, not a live split")
    if tx.status == core.Status.POSTED.value:
        ledger_service.refuse_corrected(session, tx)
    own = own_leg(tx)
    if own is None:  # unreachable: the marker names a real leg of the row
        raise SplitError("this split has no leg on its own account")
    if category_ref:
        category = ledger_service.resolve_account(session, category_ref)
        if not category.is_virtual:
            raise SplitError(f"{category.path} is not a category")
        ledger_service.refuse_system_account(category)
        ledger_service.check_category_currency(own.currency, category)
    else:
        from purser.feed.importer import _uncategorized

        category = _uncategorized(session, income=Decimal(own.amount) > 0)
    backers = current_backers(session, tx)
    ledger_service.lock_rows(session, [tx, *backers])
    specs = core.expand_flat(
        account_id=own.account_id,
        category_id=category.id,
        amount=Decimal(own.amount),
        currency=own.currency.strip(),
    )
    out = {
        "account": str(own.account.path),
        "amount": core.quantize(Decimal(own.amount)),
        "category": str(category.path),
        "backing_restored": [str(b.id) for b in backers],
    }
    if dry_run:
        return {"dry_run": True, "transaction_id": str(tx.id), **out}
    prior = _prior_statuses(session, tx.id)
    head = _write(
        session,
        tx=tx,
        specs=specs,
        split_account_id=None,
        token=token,
        action="transaction.unsplit",
        audit_extra={"backing_restored": out["backing_restored"]},
        keep_review_mark=True,
    )
    restored = _restore(session, backers, prior, token)
    events.emit(
        session,
        type_=EVENT_UNSPLIT,
        entity_type="transaction",
        entity_id=tx.id,
        payload={
            "transaction_id": str(head.id),
            "replaces_id": str(tx.id) if head.id != tx.id else None,
            "account": out["account"],
            "category": out["category"],
            "restored_ids": [str(c.id) for c in restored],
        },
    )
    session.expire(head, ["postings"])
    return {
        "transaction_id": str(head.id),
        "replaces_id": str(tx.id) if head.id != tx.id else None,
        "status": head.status,
        **out,
        "restored_ids": [str(c.id) for c in restored],
    }


def refuse_void_with_backing(session: Session, tx: Transaction) -> None:
    """A split carrying backing rows is never voided by hand: the rows it
    merged would be left pointing at a void row, their money gone from
    their accounts. Remove the split first (it restores them)."""
    if tx.split_account_id is not None and current_backers(session, tx):
        raise SplitError(
            "this split carries backing bank rows; remove the split first (it restores them)"
        )


def splits_touching(session: Session, account_id: uuid.UUID) -> list[Transaction]:
    """Live splits with a leg on ``account_id`` — the account purge removes
    them first, so the bank line on the split's own account and every backing
    row on another account survive the delete."""
    return list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.split_account_id.is_not(None),
                Transaction.status.in_(matching.MATCHABLE_STATUSES),
                Transaction.superseded_by.is_(None),
                Posting.account_id == account_id,
                bank_lineage.chain_head_clause(),
            )
            .distinct()
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Backing candidates
# ---------------------------------------------------------------------------


def backing_candidates(
    session: Session,
    *,
    tx_id: uuid.UUID,
    account_ref: str,
    amount: Decimal | None = None,
    window_days: int = BACKING_WINDOW_DAYS,
    limit: int = CANDIDATES_MAX,
) -> dict[str, Any]:
    """Bank rows on ``account_ref`` that could back an ``[Account]`` line of
    the split of ``tx_id``: live one-sided rows carrying a bank line, within
    ``window_days`` of the row's date, nearest amount (to ``amount``, the leg
    the line books, when given) then nearest date first. The rows already
    backing this split on that account come first, ``backing = true``."""
    tx = _load(session, tx_id)
    account = ledger_service.resolve_account(session, account_ref)
    if account.is_virtual:
        raise SplitError(f"{account.path} is a category; only an account line is backed")
    target = core.quantize(amount) if amount is not None else None
    window = timedelta(days=window_days)
    rows = list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Posting.account_id == account.id,
                Transaction.status.in_(matching.MATCHABLE_STATUSES),
                Transaction.superseded_by.is_(None),
                Transaction.split_account_id.is_(None),
                Transaction.id != tx.id,
                Transaction.date >= tx.date - window,
                Transaction.date <= tx.date + window,
                bank_lineage.chain_head_clause(),
            )
            .distinct()
        ).scalars()
    )
    eligible: list[Transaction] = []
    for row in rows:
        real = [p for p in row.postings if not p.account.is_virtual]
        virtual = [p for p in row.postings if p.account.is_virtual]
        if len(real) != 1 or len(virtual) != 1 or ledger_service.has_system_leg(row.postings):
            continue
        if matching.live_match_on(session, row) is not None:
            continue
        eligible.append(row)
    banked = matching.bank_line_ids(session, eligible)
    eligible = [row for row in eligible if row.id in banked]
    backing = [
        b
        for b in current_backers(session, tx)
        if any(p.account_id == account.id and not p.account.is_virtual for p in b.postings)
    ]

    def leg(row: Transaction) -> Decimal:
        return core.quantize(
            Decimal(next(p.amount for p in row.postings if p.account_id == account.id))
        )

    def key(row: Transaction) -> tuple[Decimal, int, str]:
        distance = abs(leg(row) - target) if target is not None else Decimal(0)
        return distance, abs((row.date - tx.date).days), str(row.id)

    eligible.sort(key=key)

    def item(row: Transaction, *, is_backing: bool) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "date": row.date,
            "amount": leg(row),
            "status": row.status if not is_backing else "merged",
            "payee": row.payee.name if row.payee else None,
            "description": row.description,
            "backing": is_backing,
            "exact": target is not None and leg(row) == target,
        }

    items = [item(b, is_backing=True) for b in backing] + [
        item(row, is_backing=False) for row in eligible[:limit]
    ]
    return {
        "account": str(account.path),
        "window_days": window_days,
        "amount": target,
        "candidates": items,
    }


def exact_backing(
    session: Session, tx: Transaction, account: Account, leg: Decimal, taken: set[uuid.UUID]
) -> list[uuid.UUID]:
    """The one bank row on ``account`` whose amount is exactly ``leg``, when
    exactly one is free near the row — else none. The "split like last
    time" card proposes it; it never guesses among several."""
    found = backing_candidates(
        session, tx_id=tx.id, account_ref=str(account.id), amount=leg, limit=CANDIDATES_MAX
    )
    exact = [
        uuid.UUID(c["id"])
        for c in found["candidates"]
        if c["exact"] and not c["backing"] and uuid.UUID(c["id"]) not in taken
    ]
    return exact if len(exact) == 1 else []


# ---------------------------------------------------------------------------
# The feed posts a pending backing row (decision 118)
# ---------------------------------------------------------------------------


def carry_posted_backers(
    session: Session,
    *,
    account: Account,
    still_pending_hashes: set[str],
    window_start: Any,
) -> int:
    """A bank pending merged into a split as a backing row has posted: hand
    its place to the posted row, so the money is counted once.

    A merged pending is void, so the pending lifecycle never looks at it.
    When the bank posts it under the same identity the importer skips the
    posted row (the identity is held), and nothing is counted twice. When it
    posts under a new identity the posted row lands as a new row beside the
    split's leg: this step — run beside ``reconcile_vanished_pendings`` on
    every sync — finds, for each merged pending the fetch no longer lists as
    pending, the posted row the reconciler would pick
    (``find_posted_replacement``), voids it into the split in the pending's
    place, and points the pending at it (a pending's posted successor).
    Un-split then restores the posted row. Returns how many it carried."""
    from purser.feed import reconcile

    split = aliased(Transaction)
    pendings = list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .join(split, split.id == Transaction.superseded_by)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(
                split.split_account_id.is_not(None),
                split.status.in_(matching.MATCHABLE_STATUSES),
                split.superseded_by.is_(None),
                Transaction.status == core.Status.VOID.value,
                Transaction.origin == core.Origin.IMPORT.value,
                Transaction.content_hash.is_not(None),
                Transaction.date >= window_start,
                Posting.account_id == account.id,
            )
            .distinct()
            .order_by(Transaction.date, Transaction.created_at, Transaction.id)
        ).scalars()
    )
    carried = 0
    for pending in pendings:
        if pending.content_hash in still_pending_hashes:
            continue
        head_id = pending.superseded_by
        assert head_id is not None
        if _prior_statuses(session, head_id).get(str(pending.id)) != core.Status.PENDING.value:
            continue
        successor = reconcile.find_posted_replacement(session, pending, account.id)
        if successor is None or successor.superseded_by is not None:
            continue
        if session.execute(
            select(Transaction.id).where(Transaction.reverses_id == successor.id).limit(1)
        ).first():
            continue  # corrected since it landed: leave it to the person
        if successor.split_account_id is not None or len(successor.postings) != 2:
            continue
        head = session.get(Transaction, head_id)
        assert head is not None
        ledger_service.lock_rows(session, [successor, pending, head])
        _merge_backer(session, successor, head)
        pending.superseded_by = successor.id
        session.flush()
        audit.record(
            session,
            actor_token_id=None,
            action="transaction.void",
            entity_type="transaction",
            entity_id=successor.id,
            before={"status": "posted"},
            after={
                "status": "void",
                "reason": "split_backing_posted",
                "superseded_by": str(head.id),
                "pending_id": str(pending.id),
            },
        )
        events.emit(
            session,
            type_=EVENT_BACKING_POSTED,
            entity_type="transaction",
            entity_id=head.id,
            payload={
                "seq": _next_seq(session, head.id),
                "transaction_id": str(head.id),
                "from_id": str(pending.id),
                "to_id": str(successor.id),
                "to_status": core.Status.POSTED.value,
            },
        )
        carried += 1
    return carried


# ---------------------------------------------------------------------------
# "Split like last time?" (decision 118)
# ---------------------------------------------------------------------------

# A new bank row is offered its payee's last split when that split is at
# most this old: a paycheck or a recurring bill split the same way. "Its
# payee" is the payee's ALIAS GROUP (decision 46's retroactive rename,
# ``rules.alias_group``), resolved when the sweep runs: a payroll the bank
# words three ways, consolidated under one friendly payee, is one payee
# here too, as it is for payee memory and transfer_link scopes (decision 130).
TEMPLATE_LOOKBACK_DAYS = 120
# Only rows this recent get a card, so a first pass over an imported history
# does not flood the inbox (the transfer_pair cards' rule, decision 102).
CARD_WINDOW_DAYS = 30
USER_ORIGINS = (core.Origin.HUMAN.value, core.Origin.AGENT.value)


def _payee_group(session: Session, payee_id: uuid.UUID) -> set[uuid.UUID]:
    """The payee and every payee aliased with it (``rules.alias_group``; the
    payee itself always, even on an alias chain longer than one hop)."""
    return {payee_id, *rules_service.alias_group(session, payee_id)}


def _live_split_heads() -> Any:
    return and_(
        Transaction.split_account_id.is_not(None),
        Transaction.status.in_(matching.MATCHABLE_STATUSES),
        Transaction.superseded_by.is_(None),
        bank_lineage.chain_head_clause(),
    )


def _template_for(session: Session, row: Transaction, own: Posting) -> Transaction | None:
    """The payee's most recent live split on the row's account within the
    lookback, by any payee in its alias group — only when a person or an
    agent made it (a QIF import's split is history, not a habit)."""
    assert row.payee_id is not None
    latest = session.execute(
        select(Transaction)
        .options(selectinload(Transaction.postings).selectinload(Posting.account))
        .where(
            _live_split_heads(),
            Transaction.payee_id.in_(_payee_group(session, row.payee_id)),
            Transaction.split_account_id == own.account_id,
            Transaction.id != row.id,
            Transaction.date <= row.date,
            Transaction.date >= row.date - timedelta(days=TEMPLATE_LOOKBACK_DAYS),
        )
        .order_by(Transaction.date.desc(), Transaction.created_at.desc(), Transaction.id)
        .limit(1)
    ).scalar_one_or_none()
    if latest is None or latest.origin not in USER_ORIGINS:
        return None
    return latest


def draft_lines_for(
    session: Session, row: Transaction, own: Posting, template: Transaction
) -> list[dict[str, Any]] | None:
    """The template's lines for ``row``: the amount difference goes to the
    largest category line; account lines keep their amounts and name the one
    exact backing row, when there is exactly one. None when the lines cannot
    add up (no category line to take a difference, or fewer than two lines
    left)."""
    template_own = own_leg(template)
    if template_own is None:
        return None
    lines = [dict(line) for line in describe_lines(session, template)]
    difference = core.quantize(Decimal(own.amount)) - core.quantize(Decimal(template_own.amount))
    if difference != 0:
        categories = [line for line in lines if line["kind"] == "category"]
        if not categories:
            return None
        largest = max(categories, key=lambda line: abs(line["amount"]))
        largest["amount"] = largest["amount"] + difference
        if largest["amount"] == 0:
            lines.remove(largest)
    if len(lines) < 2:
        return None
    taken: set[uuid.UUID] = set()
    out = []
    for line in lines:
        backing: list[uuid.UUID] = []
        if line["kind"] == "account":
            account = ledger_service.resolve_account(session, line["target"][1:-1])
            backing = exact_backing(session, row, account, -line["amount"], taken)
            taken.update(backing)
        out.append(
            {
                "target": line["target"],
                "amount": str(line["amount"]),
                "memo": line["memo"],
                "backing_ids": [str(i) for i in backing],
            }
        )
    return out


def _splittable_bank_rows(session: Session, since: Any) -> list[Transaction]:
    """Posted plain bank rows from the feed (their lineage starts at a
    downloaded row) that automation left as they came, dated since
    ``since``, whose payee's alias group has a person's or an agent's split."""
    split_payees = session.execute(
        select(Transaction.payee_id)
        .where(_live_split_heads(), Transaction.origin.in_(USER_ORIGINS))
        .distinct()
    ).scalars()
    payees: set[uuid.UUID] = set()
    for payee_id in split_payees:
        if payee_id is not None:
            payees |= _payee_group(session, payee_id)
    if not payees:
        return []
    rows = list(
        session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.status == core.Status.POSTED.value,
                Transaction.superseded_by.is_(None),
                Transaction.split_account_id.is_(None),
                Transaction.date >= since,
                Transaction.payee_id.in_(payees),
                Transaction.origin.in_(
                    (core.Origin.IMPORT.value, core.Origin.RULE.value, core.Origin.SYSTEM.value)
                ),
                bank_lineage.chain_head_clause(),
            )
            .order_by(Transaction.date, Transaction.created_at, Transaction.id)
        ).scalars()
    )
    if not rows:
        return []
    roots = bank_lineage.lineage_roots(session, [r.id for r in rows])
    downloaded = set(
        session.execute(
            select(Transaction.id).where(
                Transaction.id.in_(set(roots.values())),
                Transaction.origin == core.Origin.IMPORT.value,
                Transaction.source_fetch_id.is_not(None),
            )
        ).scalars()
    )
    return [
        r
        for r in rows
        if roots[r.id] in downloaded and not ledger_service.has_system_leg(r.postings)
    ]


def _rejected_for_payee(session: Session, account_id: uuid.UUID, payee_id: uuid.UUID) -> bool:
    """A "never for this payee on this account" rejection holds for the
    payee's whole alias group, compared when the sweep runs: an alias made
    after the rejection still stops its rows."""
    return (
        session.execute(
            select(Suggestion.id)
            .where(
                Suggestion.kind == suggestions_service.KIND_SPLIT,
                Suggestion.status == suggestions_service.STATUS_REJECTED,
                Suggestion.reject_scope == "counterparty",
                Suggestion.subject_account_id == account_id,
                Suggestion.subject_payee_id.in_(_payee_group(session, payee_id)),
            )
            .limit(1)
        ).first()
        is not None
    )


def _still_splittable(session: Session, card: Suggestion) -> bool:
    draft = suggestions_service.SplitDraft.model_validate(card.draft)
    head_id = bank_lineage.current_rows(session, [draft.transaction_id])[draft.transaction_id]
    head = session.get(Transaction, head_id)
    if head is None or head.status != core.Status.POSTED.value or head.superseded_by is not None:
        return False
    if head.split_account_id is not None:
        return False
    if session.execute(
        select(Transaction.id).where(Transaction.reverses_id == head.id).limit(1)
    ).first():
        return False
    return own_leg(_load(session, head.id)) is not None


def sync_split_suggestions(
    session: Session, *, actor_token_id: uuid.UUID | None = None
) -> dict[str, int]:
    """Propose "split like last time?" for each new bank row whose payee's
    (alias group's) most recent split on that account a person or an agent
    made, and withdraw open cards whose row was split, linked, corrected or
    voided since. Runs after every feed import and in the daily matching job; never
    writes to the ledger (decision 118: a split is never automatic)."""
    from purser import clock

    stats = {"split_cards_created": 0, "split_cards_withdrawn": 0}
    open_cards = list(
        session.execute(
            select(Suggestion).where(
                Suggestion.kind == suggestions_service.KIND_SPLIT,
                Suggestion.status == suggestions_service.STATUS_OPEN,
            )
        ).scalars()
    )
    for card in open_cards:
        if not _still_splittable(session, card) and suggestions_service._close_card(
            session,
            card,
            status=suggestions_service.STATUS_WITHDRAWN,
            actor_token_id=actor_token_id,
            reason="no_longer_splittable",
        ):
            stats["split_cards_withdrawn"] += 1
    since = clock.ledger_today() - timedelta(days=CARD_WINDOW_DAYS)
    rows = _splittable_bank_rows(session, since)
    roots = bank_lineage.correction_roots(session, [r.id for r in rows])
    for row in rows:
        fingerprint = suggestions_service.split_fingerprint(roots[row.id])
        if session.execute(
            select(Suggestion.id).where(Suggestion.fingerprint == fingerprint)
        ).first():
            continue
        own = own_leg(row)
        if own is None or row.payee_id is None:
            continue
        if _rejected_for_payee(session, own.account_id, row.payee_id):
            continue
        template = _template_for(session, row, own)
        if template is None:
            continue
        lines = draft_lines_for(session, row, own, template)
        if lines is None:
            continue
        template_own = own_leg(template)
        assert template_own is not None
        draft = suggestions_service.SplitDraft.model_validate(
            {
                "transaction_id": str(row.id),
                "template_id": str(template.id),
                "account": str(own.account.path),
                "date": row.date.isoformat(),
                "amount": str(core.quantize(Decimal(own.amount))),
                "template_amount": str(core.quantize(Decimal(template_own.amount))),
                "payee": row.payee.name if row.payee else None,
                "lines": lines,
            }
        )
        _, created = suggestions_service._create_once(
            session,
            kind=suggestions_service.KIND_SPLIT,
            draft=draft.stored(),
            source=suggestions_service.SOURCE_DETECTED,
            fingerprint=fingerprint,
            subject_account_id=own.account_id,
            subject_payee_id=row.payee_id,
            counterparty_account_id=None,
            evidence={
                "occurrences": [
                    {
                        "date": template.date.isoformat(),
                        "amount": str(core.quantize(Decimal(template_own.amount))),
                    }
                ]
            },
            confidence=None,
            detector=suggestions_service.SPLIT_DETECTOR,
            detector_version=1,
            actor_token_id=actor_token_id,
            event_payload={"transaction_id": str(row.id), "template_id": str(template.id)},
        )
        if created:
            stats["split_cards_created"] += 1
    return stats
