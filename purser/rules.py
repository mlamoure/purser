"""Deterministic rule engine — loading, evaluation, application (issue #174,
DESIGN decision 46).

The *predicate* lives in ``purser.core.rules`` (pure, mypy --strict). This
module is the service layer around it: it loads the active set, decrypts the
narrative columns once per pass, evaluates in order, and applies the action.

Two invariants worth stating up front, because both are structural rather
than conventional:

- **A draft never executes.** ``load_active`` is the only loader and filters
  ``state='active'``. A rule is a draft unless its creator asks for
  ``activate`` (#436), so an agent acting on its own initiative authors
  rules that do not run until the user activates them.
- **Rules run strictly AFTER content hashing.** The importer hashes, dedups,
  then shapes. So activating, editing or deleting a rule can never re-import
  history or disturb ``_current_pending_hashes`` (decision 43's hash-on-raw
  guarantee).
"""

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, or_, select, tuple_
from sqlalchemy.orm import Session, selectinload

from purser import audit, crypto
from purser.core import ledger as core_ledger
from purser.core import matching as core_matching
from purser.core import rules as core_rules
from purser.logging import get_logger
from purser.models import ApiToken, Payee, Posting, Rule, Transaction

if TYPE_CHECKING:
    from purser.transfers import PlannedAbsorb, PlannedMerge

log = get_logger(__name__)

TYPE_CATEGORIZE = "categorize"
TYPE_RENAME = "rename"
TYPE_TRANSFER_LINK = "transfer_link"

STATE_DRAFT = "draft"
STATE_ACTIVE = "active"
STATE_DISABLED = "disabled"
STATE_ARCHIVED = "archived"

# A payee alias chain should be one hop in practice; the bound stops a cycle
# the DB CHECK cannot see (it only rejects direct self-reference) from
# spinning a read forever.
_MAX_ALIAS_DEPTH = 10


@dataclass(frozen=True)
class LoadedRule:
    """One active rule with its narrative columns already decrypted."""

    id: uuid.UUID
    type: str
    priority: int
    predicate: core_rules.Predicate
    category_id: uuid.UUID | None
    payee_name: str | None
    pair: frozenset[uuid.UUID] | None
    amount_exact: Decimal | None
    effective_from: date_type | None
    window_days: int | None = None  # transfer_link only; None = the global window
    # transfer_link only (decision 67): the scope payee's alias group, resolved
    # at load time; None = the whole pair.
    payee_scope: frozenset[uuid.UUID] | None = None


def payee_scope_group(session: Session, payee_id: uuid.UUID | None) -> frozenset[uuid.UUID] | None:
    """The payee ids a transfer_link rule's ``payee_scope_id`` covers: its
    whole ALIAS GROUP, resolved now rather than when the rule was written —
    so a rename whose apply-history aliases the payee (or aliases other
    descriptors onto it) keeps the rule in scope instead of silently
    disabling it, the failure that sank #253's exact-``payee_id`` memory
    (decision 67)."""
    if payee_id is None:
        return None
    return frozenset(alias_group(session, payee_id))


def _predicate_of(row: Rule) -> core_rules.Predicate:
    return core_rules.Predicate(
        account_ids=(
            frozenset(str(a) for a in row.account_ids) if row.account_ids is not None else None
        ),
        match_field=core_rules.MatchField(row.match_field) if row.match_field else None,
        match_mode=core_rules.MatchMode(row.match_mode) if row.match_mode else None,
        match_text=row.match_text,
        amount_exact=row.amount_exact,
        amount_min=row.amount_min,
        amount_max=row.amount_max,
        direction=core_rules.Direction(row.direction) if row.direction else None,
        effective_from=row.effective_from,
    )


def load_active(session: Session) -> list[LoadedRule]:
    """The active set in evaluation order: (priority, created_at, id).

    THE only loader. Draft, disabled and archived rules are filtered here, so
    no caller can accidentally execute one. Decryption happens once per pass —
    rules number in the tens, and `prefix`/`contains` need the plaintext.
    """
    rows = list(
        session.execute(
            select(Rule)
            .where(Rule.state == STATE_ACTIVE)
            .order_by(Rule.priority, Rule.created_at, Rule.id)
        ).scalars()
    )
    loaded: list[LoadedRule] = []
    for row in rows:
        pair: frozenset[uuid.UUID] | None = None
        if row.pair_account_a is not None and row.pair_account_b is not None:
            pair = frozenset({row.pair_account_a, row.pair_account_b})
        loaded.append(
            LoadedRule(
                id=row.id,
                type=row.type,
                priority=row.priority,
                predicate=_predicate_of(row),
                category_id=row.category_id,
                payee_name=row.payee_name,
                pair=pair,
                amount_exact=row.amount_exact,
                effective_from=row.effective_from,
                window_days=row.window_days,
                payee_scope=payee_scope_group(session, row.payee_scope_id),
            )
        )
    return loaded


def first_match(
    rules: list[LoadedRule], type_: str, candidate: core_rules.Candidate
) -> LoadedRule | None:
    """First match wins, in the loaded order.

    Priority, not most-specific-wins: specificity would be a second, hidden
    ranking function whose meaning shifts when an unrelated predicate field is
    edited, and "why did this rule win" has to be answerable by pointing at
    one row.
    """
    for rule in rules:
        if rule.type != type_:
            continue
        if core_rules.matches(rule.predicate, candidate):
            return rule
    return None


# --------------------------------------------------------------------------
# transfer_link — consulted by transfers.auto_link_pass
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredPair:
    rule_id: uuid.UUID
    accounts: frozenset[uuid.UUID]
    amount_exact: Decimal | None
    effective_from: date_type | None
    window_days: int  # effective: the rule's own window, else the global one
    # The scope payee's alias group (decision 67): a pair is covered only when
    # either leg's payee is in it. None = the whole pair.
    payees: frozenset[uuid.UUID] | None = None


def effective_window_days(window_days: int | None) -> int:
    """A transfer_link rule's date window: its own ``window_days``, or the
    global ±TRANSFER_WINDOW_DAYS when it sets none (decision 66)."""
    return core_matching.TRANSFER_WINDOW_DAYS if window_days is None else window_days


def validate_window_days(window_days: object, *, rule_type: str | None) -> None:
    """The one check every boundary shares (REST, MCP, service): a whole
    number of days within 0..TRANSFER_WINDOW_MAX_DAYS, on a transfer_link
    rule only. ``rule_type=None`` skips the type test (a pair preview has no
    rule yet). The DB CHECK ``rules_window_shape`` is the backstop."""
    if window_days is None:
        return
    # bool is an int subclass; True is not "one day".
    if isinstance(window_days, bool) or not isinstance(window_days, int):
        raise RuleError("window_days must be a whole number of days")
    if rule_type is not None and rule_type != TYPE_TRANSFER_LINK:
        raise RuleError("window_days applies only to transfer_link rules")
    if not 0 <= window_days <= core_matching.TRANSFER_WINDOW_MAX_DAYS:
        raise RuleError(
            f"window_days must be between 0 and {core_matching.TRANSFER_WINDOW_MAX_DAYS}"
        )


def declared_pairs(rules: list[LoadedRule]) -> list[DeclaredPair]:
    """transfer_link rules are UNORDERED — they carry no conflicting action,
    so a pair is covered if *any* active rule covers it (set union)."""
    return [
        DeclaredPair(
            rule_id=r.id,
            accounts=r.pair,
            amount_exact=r.amount_exact,
            effective_from=r.effective_from,
            window_days=effective_window_days(r.window_days),
            payees=r.payee_scope,
        )
        for r in rules
        if r.type == TYPE_TRANSFER_LINK and r.pair is not None
    ]


def _covering(
    pairs: list[DeclaredPair],
    account_a: uuid.UUID,
    account_b: uuid.UUID,
    amount: Decimal,
    when: date_type,
    payee_ids: Iterable[uuid.UUID | None],
) -> list[DeclaredPair]:
    """Declarations covering this account pair, amount, date and payees —
    every condition except the window. ``amount`` is compared on abs(), a pair
    being inherently ±; ``when`` is the outgoing (or absorbed) row's date;
    ``payee_ids`` are the two legs' payees, and a payee-scoped declaration
    covers the pair when either is in its alias group (decision 67)."""
    wanted = frozenset({account_a, account_b})
    payees = {p for p in payee_ids if p is not None}
    return [
        pair
        for pair in pairs
        if pair.accounts == wanted
        and (pair.effective_from is None or when >= pair.effective_from)
        and (pair.amount_exact is None or abs(amount) == abs(pair.amount_exact))
        and (pair.payees is None or not pair.payees.isdisjoint(payees))
    ]


def pair_declared(
    pairs: list[DeclaredPair],
    account_a: uuid.UUID,
    account_b: uuid.UUID,
    amount: Decimal,
    when: date_type,
    *,
    gap_days: int,
    payee_ids: Iterable[uuid.UUID | None] = (),
) -> uuid.UUID | None:
    """The first rule declaring this account pair for legs ``gap_days`` apart,
    or None.

    Replaces ONLY the name-signal test in ``auto_link_pass``, plus — for its
    own pair — the date window (decision 66): a rule vouches for a gap up to
    its ``window_days``, and a payee-scoped rule only for legs carrying its
    payee or an alias (decision 67; with no ``payee_ids`` it covers nothing).
    Every other condition — exact opposite amount, posted-only,
    cross-account, and unlink suppression — still applies and is checked by
    the caller (DESIGN §5).
    """
    for pair in _covering(pairs, account_a, account_b, amount, when, payee_ids):
        if gap_days <= pair.window_days:
            return pair.rule_id
    return None


def declared_window(
    pairs: list[DeclaredPair],
    account_a: uuid.UUID,
    account_b: uuid.UUID,
    amount: Decimal,
    when: date_type,
    *,
    payee_ids: Iterable[uuid.UUID | None] = (),
) -> int | None:
    """The widest window any declaration grants this pair, or None when no
    rule declares it — the set union, applied to windows."""
    windows = [
        p.window_days for p in _covering(pairs, account_a, account_b, amount, when, payee_ids)
    ]
    return max(windows) if windows else None


# --------------------------------------------------------------------------
# Import-time shaping: rename -> categorize, before payee memory
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Shaped:
    """What the rule pass decided about a row about to be created."""

    payee_name: str | None
    category_id: uuid.UUID | None
    applied: list[uuid.UUID]


def shape_new_row(
    rules: list[LoadedRule],
    *,
    account_id: uuid.UUID,
    amount: Decimal,
    when: date_type,
    payee_name: str | None,
    description: str | None,
) -> Shaped:
    """Apply rename then categorize to a row that has not been created yet.

    Order is load-bearing in both directions. Rename runs FIRST so the
    friendly payee is what ``category_for_new_row`` looks memory up on —
    several raw descriptors renamed onto one payee make that payee's memory
    stronger, since ``payee_memory`` counts posted rows per ``payee_id``. And
    the whole pass runs after hashing, so identity is untouched.
    """
    applied: list[uuid.UUID] = []
    candidate = core_rules.Candidate(
        account_id=str(account_id),
        amount=amount,
        date=when,
        payee=payee_name,
        description=description,
    )

    rename = first_match(rules, TYPE_RENAME, candidate)
    if rename is not None and rename.payee_name:
        payee_name = rename.payee_name
        applied.append(rename.id)
        # Re-derive the candidate: a categorize rule matching on payee must
        # see the RENAMED payee, or the two rule types disagree about what the
        # row is.
        candidate = core_rules.Candidate(
            account_id=str(account_id),
            amount=amount,
            date=when,
            payee=payee_name,
            description=description,
        )

    category_id: uuid.UUID | None = None
    categorize = first_match(rules, TYPE_CATEGORIZE, candidate)
    if categorize is not None and categorize.category_id is not None:
        category_id = categorize.category_id
        applied.append(categorize.id)

    return Shaped(payee_name=payee_name, category_id=category_id, applied=applied)


def mark_applied(session: Session, rule_ids: list[uuid.UUID]) -> None:
    """Bump ``applied_count`` / ``last_applied_at`` for rules that fired."""
    if not rule_ids:
        return
    now = datetime.now(UTC)
    for rule_id in set(rule_ids):
        row = session.get(Rule, rule_id)
        if row is not None:
            row.applied_count += 1
            row.last_applied_at = now


# --------------------------------------------------------------------------
# Payee aliasing (retroactive rename — decision 46, Q1)
# --------------------------------------------------------------------------


def resolve_payee_id(session: Session, payee_id: uuid.UUID | None) -> uuid.UUID | None:
    """Follow ``merged_into_id`` to the surviving payee.

    Depth-bounded rather than trusting the data: the DB CHECK only rejects
    direct self-reference, so a longer cycle would otherwise spin forever.
    """
    if payee_id is None:
        return None
    seen: set[uuid.UUID] = set()
    current = payee_id
    for _ in range(_MAX_ALIAS_DEPTH):
        if current in seen:
            log.warning("rules.payee_alias_cycle", payee_id=str(payee_id))
            return current
        seen.add(current)
        row = session.get(Payee, current)
        if row is None or row.merged_into_id is None:
            return current
        current = row.merged_into_id
    log.warning("rules.payee_alias_too_deep", payee_id=str(payee_id))
    return current


def alias_group(session: Session, payee_id: uuid.UUID) -> list[uuid.UUID]:
    """Every payee id that resolves to the same surviving payee.

    ``alias_payee`` always points at the RESOLVED target, so chains stay one
    hop deep and "the target plus everything pointing directly at it" is
    complete. This is what makes a retroactive rename strengthen payee memory:
    the aggregate counts posted rows across the whole group, not just the
    friendly payee's own rows.
    """
    target = resolve_payee_id(session, payee_id)
    if target is None:
        return []
    children = list(
        session.execute(select(Payee.id).where(Payee.merged_into_id == target)).scalars()
    )
    return [target, *children]


def alias_payee(
    session: Session,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    token: ApiToken | None,
) -> None:
    """Point ``source`` at ``target``. Touches NO transaction row.

    This is the whole reason retroactive rename works: migration 0005's
    allow-list makes ``transactions.payee_id`` immutable on posted rows, so
    re-pointing history is structurally impossible on exactly the rows a
    rename wants to consolidate. Aliasing the entity engages no guard, mints
    no reversal pair, and is reversible by clearing the column.
    """
    if source_id == target_id:
        raise ValueError("a payee cannot be aliased to itself")
    source = session.get(Payee, source_id)
    target = session.get(Payee, target_id)
    if source is None or target is None:
        raise ValueError("payee not found")
    # Aliasing onto something that is itself aliased would build a chain the
    # depth bound then has to walk; collapse to the surviving payee instead.
    resolved_target = resolve_payee_id(session, target_id)
    if resolved_target == source_id:
        raise ValueError("that alias would create a cycle")
    source.merged_into_id = resolved_target
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="payee.merge",
        entity_type="payee",
        entity_id=source_id,
        before={"merged_into_id": None},
        after={"merged_into_id": str(resolved_target)},
    )


def blind_index_for(pattern: str) -> str:
    """The dedup index for a rule's match text — same normalization the
    predicate applies, so two rules that would match identically collide."""
    return crypto.blind_index(core_ledger.normalize_payee(pattern))


# --------------------------------------------------------------------------
# CRUD + lifecycle
# --------------------------------------------------------------------------


class RuleError(Exception):
    """A rule request the service refuses (bad shape, bad transition)."""


_EDITABLE = {
    "type",
    "priority",
    "account_ids",
    "match_field",
    "match_mode",
    "match_text",
    "amount_exact",
    "amount_min",
    "amount_max",
    "direction",
    "effective_from",
    "category_id",
    "payee_name",
    "pair_account_a",
    "pair_account_b",
    "window_days",
    "payee_scope_id",
    "note",
}

# What a transfer_link rule links: its pair and everything that narrows or
# widens it. Frozen while the rule is active (decision 66) — an edit here
# reaches into history exactly like an activation, so it goes through the
# activation confirm instead of around it. Dropping a payee scope (decision
# 67) widens the rule to the whole pair, so the scope is frozen too.
_PAIR_SCOPE = (
    "type",
    "pair_account_a",
    "pair_account_b",
    "amount_exact",
    "effective_from",
    "window_days",
    "payee_scope_id",
)


def validate_payee_scope(
    session: Session, payee_scope_id: object, *, rule_type: str | None
) -> None:
    """A payee scope narrows a declared pair, so only a transfer_link rule
    carries one, and it must name a payee that exists (decision 67). The DB
    CHECK ``rules_payee_scope_shape`` and the FK are the backstop."""
    if payee_scope_id is None:
        return
    if not isinstance(payee_scope_id, uuid.UUID):
        raise RuleError("payee_scope_id must be a payee id")
    if rule_type is not None and rule_type != TYPE_TRANSFER_LINK:
        raise RuleError("a payee scope applies only to transfer_link rules")
    if session.get(Payee, payee_scope_id) is None:
        raise RuleError("payee_scope_id names no payee")


def _shape(rule: Rule) -> dict[str, Any]:
    """The audit row's view of a rule. A transfer_link rule's window and payee
    scope ride along: widening either widens a drift-blind operation, so who
    set it and when must be answerable from the audit log."""
    shape: dict[str, Any] = {"type": rule.type, "state": rule.state, "priority": rule.priority}
    if rule.type == TYPE_TRANSFER_LINK:
        shape["window_days"] = effective_window_days(rule.window_days)
        if rule.payee_scope_id is not None:
            shape["payee_scope_id"] = str(rule.payee_scope_id)
    return shape


def _audit(
    session: Session,
    *,
    action: str,
    rule: Rule,
    token: ApiToken | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action=action,
        entity_type="rule",
        entity_id=rule.id,
        before=before,
        after=after or _shape(rule),
    )


def create(
    session: Session,
    *,
    fields: dict[str, object],
    token: ApiToken | None,
    activate: bool = False,
) -> Rule:
    """Create a rule: a draft unless ``activate``. The caller checks the
    ``write`` scope (the API dependency), not here."""
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise RuleError(f"unknown rule fields: {sorted(unknown)}")
    if activate and fields.get("type") == TYPE_TRANSFER_LINK:
        # A pair rule links history the moment it is active, and a merge is
        # drift-blind, so its activation always confirms the projected link
        # count (set_state, decision 46 Q2). Creating it active would skip
        # that confirmation (#436 made activate-on-create reachable).
        raise RuleError(
            "a transfer_link rule is created as a draft: activate it with its projected "
            "link count confirmed (POST /rules/{id}/state with confirm_links)"
        )
    validate_window_days(fields.get("window_days"), rule_type=str(fields.get("type")))
    validate_payee_scope(session, fields.get("payee_scope_id"), rule_type=str(fields.get("type")))
    rule = Rule(**fields)
    rule.state = STATE_ACTIVE if activate else STATE_DRAFT
    rule.created_by = token.id if token else None
    if rule.match_text:
        rule.match_text_bidx = blind_index_for(rule.match_text)
    session.add(rule)
    session.flush()
    _audit(session, action="rule.create", rule=rule, token=token)
    return rule


def update(
    session: Session, *, rule_id: uuid.UUID, changes: dict[str, object], token: ApiToken | None
) -> Rule:
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise RuleError(f"unknown rule fields: {sorted(unknown)}")
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise RuleError("rule not found")
    if rule.state == STATE_ARCHIVED:
        raise RuleError("archived rules are immutable; create a new one")
    new_type = str(changes.get("type", rule.type))
    if rule.state == STATE_ACTIVE and TYPE_TRANSFER_LINK in (rule.type, new_type):

        def scope(key: str, value: object) -> object:
            # An explicit 3 and an unset window are the same window.
            if key == "window_days" and (value is None or isinstance(value, int)):
                return effective_window_days(value)
            return value

        moved = [
            key
            for key in _PAIR_SCOPE
            if key in changes and scope(key, changes[key]) != scope(key, getattr(rule, key))
        ]
        if moved:
            raise RuleError(
                f"this transfer_link rule is active, and changing {', '.join(moved)} would "
                "link history without the activation confirm; disable it, edit it, then "
                "activate it again to confirm the projected link count"
            )
    validate_window_days(changes.get("window_days", rule.window_days), rule_type=new_type)
    validate_payee_scope(
        session, changes.get("payee_scope_id", rule.payee_scope_id), rule_type=new_type
    )
    before = _shape(rule)
    for key, value in changes.items():
        setattr(rule, key, value)
    if "match_text" in changes:
        rule.match_text_bidx = blind_index_for(rule.match_text) if rule.match_text else None
    rule.updated_at = datetime.now(UTC)
    session.flush()
    _audit(session, action="rule.update", rule=rule, token=token, before=before)
    return rule


def set_state(
    session: Session,
    *,
    rule_id: uuid.UUID,
    state: str,
    token: ApiToken | None,
    confirmed_links: int | None = None,
) -> Rule:
    """Move a rule through draft -> active -> disabled/archived.

    Activating a ``transfer_link`` rule requires an explicit confirmation of
    the projected link count (decision 46, Q2). It is retroactive for free —
    ``auto_link_pass`` sweeps all live one-sided posted history nightly, so an
    activated rule simply starts being true — and a merge is balance-neutral,
    so §7.3's drift check cannot detect a bad one. Unbounded activation is
    that drift-blind operation applied to all history at once. The count is
    taken with the rule's own ``window_days`` (decision 66): a wider window
    puts more history in scope, which is exactly what the confirm is for.
    """
    if state not in {STATE_DRAFT, STATE_ACTIVE, STATE_DISABLED, STATE_ARCHIVED}:
        raise RuleError(f"unknown state {state!r}")
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise RuleError("rule not found")
    before: dict[str, Any] = {"state": rule.state}
    if state == STATE_ACTIVE and rule.type == TYPE_TRANSFER_LINK:
        projected = project_links(session, rule)
        if confirmed_links is None or confirmed_links != projected:
            window = effective_window_days(rule.window_days)
            raise RuleError(
                f"activating this transfer_link rule would link {projected} historical "
                f"pair(s) with legs up to {window} days apart; re-send with "
                f"confirm_links={projected} to proceed"
            )
    rule.state = state
    rule.updated_at = datetime.now(UTC)
    session.flush()
    action = {
        STATE_ACTIVE: "rule.activate",
        STATE_DISABLED: "rule.disable",
        STATE_ARCHIVED: "rule.archive",
        STATE_DRAFT: "rule.update",
    }[state]
    _audit(session, action=action, rule=rule, token=token, before=before)
    return rule


# --------------------------------------------------------------------------
# Preview / projection / apply-history
# --------------------------------------------------------------------------

# Preview decrypt-and-filters a bounded sample of history (the measured
# ceiling in #141): it is a sample by design and says so. Apply-history walks
# ALL of it in keyset batches (#356), because a retroactive rule that silently
# stops at the newest N rows is wrong, not slow.
_SCAN_CAP = 5000
_HISTORY_BATCH = 1000
_SAMPLE_CAP = 20


def _live_heads() -> Select[Transaction]:
    """Live chain heads, newest first — the same pair of conditions
    categorize._uncategorized_rows uses, minus the uncategorized filter."""
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    return (
        select(Transaction)
        .options(
            selectinload(Transaction.postings).selectinload(Posting.account),
            selectinload(Transaction.payee),
        )
        .where(
            Transaction.status.in_(["pending", "posted"]),
            Transaction.superseded_by.is_(None),
            Transaction.id.not_in(reversed_ids),
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )


def _live_rows(session: Session) -> list[Transaction]:
    """The newest ``_SCAN_CAP`` live rows: preview's sample window."""
    return list(session.execute(_live_heads().limit(_SCAN_CAP)).scalars())


def _live_row_batches(session: Session) -> Iterator[list[Transaction]]:
    """Every live row, newest first, in keyset batches of ``_HISTORY_BATCH``.

    The cursor is the last row's ``(date, id)`` — the sort key itself — so no
    row is read twice or skipped however large the ledger is, and each batch
    is one query plus its eager loads rather than the whole history in memory.
    """
    cursor: tuple[date_type, uuid.UUID] | None = None
    while True:
        stmt = _live_heads().limit(_HISTORY_BATCH)
        if cursor is not None:
            stmt = stmt.where(tuple_(Transaction.date, Transaction.id) < cursor)
        batch = list(session.execute(stmt).scalars())
        if batch:
            yield batch
        if len(batch) < _HISTORY_BATCH:
            return
        cursor = (batch[-1].date, batch[-1].id)


def _candidate_of(tx: Transaction) -> core_rules.Candidate | None:
    real = next((p for p in tx.postings if not p.account.is_virtual), None)
    if real is None:
        return None
    return core_rules.Candidate(
        account_id=str(real.account_id),
        amount=Decimal(real.amount),
        date=tx.date,
        payee=tx.payee.name if tx.payee else None,
        description=tx.description,
    )


def preview(
    session: Session, *, predicate: core_rules.Predicate, limit: int = _SAMPLE_CAP
) -> dict[str, object]:
    """Dry-run a predicate over history: how many live rows it would touch,
    with a small sample. The count is exact over the scanned window and
    ``scanned``/``scan_capped`` say so rather than implying totality."""
    rows = _live_rows(session)
    matched: list[Transaction] = []
    for tx in rows:
        candidate = _candidate_of(tx)
        if candidate is not None and core_rules.matches(predicate, candidate):
            matched.append(tx)
    sample = [
        {
            "id": str(tx.id),
            "date": tx.date.isoformat(),
            "payee": tx.payee.name if tx.payee else None,
            "description": tx.description,
        }
        for tx in matched[:limit]
    ]
    return {
        "matched": len(matched),
        "scanned": len(rows),
        "scan_capped": len(rows) >= _SCAN_CAP,
        "sample": sample,
    }


def _projection(session: Session, candidate: DeclaredPair) -> "list[PlannedAbsorb | PlannedMerge]":
    """The links the next auto-link pass would make because of ``candidate``.

    Runs the pass's own selection (``transfers.plan_auto_links``) — never a
    parallel re-implementation that could drift from it — over the active
    declarations plus ``candidate``, listed FIRST so every link it covers is
    credited to it. Absorbs count as well as merges: a declared pair also
    claims bank rows into an existing transfer's open side.
    """
    from purser import transfers as transfers_service

    others = [p for p in declared_pairs(load_active(session)) if p.rule_id != candidate.rule_id]
    plan = transfers_service.plan_auto_links(session, [candidate, *others])
    ops: list[PlannedAbsorb | PlannedMerge] = [*plan.absorbs, *plan.merges]
    return [op for op in ops if op.rule_id == candidate.rule_id]


def _declared_of(session: Session, rule: Rule) -> DeclaredPair | None:
    if rule.type != TYPE_TRANSFER_LINK:
        return None
    if rule.pair_account_a is None or rule.pair_account_b is None:
        return None
    return DeclaredPair(
        rule_id=rule.id,
        accounts=frozenset({rule.pair_account_a, rule.pair_account_b}),
        amount_exact=rule.amount_exact,
        effective_from=rule.effective_from,
        window_days=effective_window_days(rule.window_days),
        payees=payee_scope_group(session, rule.payee_scope_id),
    )


def project_links(session: Session, rule: Rule) -> int:
    """How many links activating this transfer_link rule would make on the
    next pass — merges of two one-sided rows and absorbs into an existing
    transfer — within the rule's own window (decision 66) and payee scope
    (decision 67).

    It is the activation confirm's number, and it is computed from the plan
    the pass then executes, so the number the user confirms is the number
    they get.
    """
    candidate = _declared_of(session, rule)
    return 0 if candidate is None else len(_projection(session, candidate))


def _link_sample(op: "PlannedAbsorb | PlannedMerge") -> dict[str, object]:
    from purser import matching as matching_service
    from purser import transfers as transfers_service

    if isinstance(op, transfers_service.PlannedAbsorb):
        return {
            "kind": "absorb",
            "row_id": str(op.row.id),
            "transfer_id": str(op.leg.transfer.id),
            "dates": [op.row.date.isoformat(), op.leg.transfer.date.isoformat()],
            "amount": str(abs(op.leg.amount)),
            "gap_days": op.gap_days,
        }
    out_leg = matching_service._real_leg(op.outgoing)
    assert out_leg is not None  # a planned merge's rows are one-sided
    return {
        "kind": "merge",
        "outgoing_id": str(op.outgoing.id),
        "incoming_id": str(op.incoming.id),
        "dates": [op.outgoing.date.isoformat(), op.incoming.date.isoformat()],
        "amount": str(abs(Decimal(out_leg.amount))),
        "gap_days": op.gap_days,
    }


def preview_links(
    session: Session,
    *,
    pair_account_a: uuid.UUID,
    pair_account_b: uuid.UUID,
    amount_exact: Decimal | None = None,
    effective_from: date_type | None = None,
    window_days: int | None = None,
    limit: int = _SAMPLE_CAP,
) -> dict[str, object]:
    """A transfer_link rule previewed before it exists: the projection the
    activation confirm runs, so ``links`` is the number that confirm will ask
    for over the same history, with a small sample of what would link."""
    validate_window_days(window_days, rule_type=None)
    if pair_account_a == pair_account_b:
        raise RuleError("a transfer_link pair needs two different accounts")
    candidate = DeclaredPair(
        rule_id=uuid.uuid4(),  # a stand-in id: nothing is created
        accounts=frozenset({pair_account_a, pair_account_b}),
        amount_exact=amount_exact,
        effective_from=effective_from,
        window_days=effective_window_days(window_days),
    )
    links = _projection(session, candidate)
    return {
        "type": TYPE_TRANSFER_LINK,
        "window_days": candidate.window_days,
        "links": len(links),
        "sample": [_link_sample(op) for op in links[:limit]],
    }


def _automation_categorized(session: Session, tx_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Rows whose current category was set by automation, not a human.

    A `transaction.categorize` audit row exists iff some pass (deterministic
    memory, the AI classifier, or a rule) set it — a human PATCH audits as an
    update instead. That is the whole distinction apply-history needs:
    automation correcting automation is fine; automation overriding a human is
    opt-in (decision 46, Q3).
    """
    if not tx_ids:
        return set()
    from purser.models import AuditLog

    rows = session.execute(
        select(AuditLog.entity_id, AuditLog.replacement_id).where(
            AuditLog.action == "transaction.categorize",
            or_(AuditLog.entity_id.in_(tx_ids), AuditLog.replacement_id.in_(tx_ids)),
        )
    ).all()
    wanted = set(tx_ids)
    touched: set[uuid.UUID] = set()
    for entity_id, replacement_id in rows:
        for candidate in (entity_id, replacement_id):
            if candidate in wanted:
                touched.add(candidate)
    return touched


def apply_history(
    session: Session,
    *,
    rule_id: uuid.UUID,
    token: ApiToken | None,
    dry_run: bool = True,
    posted: str = "skip",
    include_user_categorized: bool = False,
) -> dict[str, Any]:
    """Retroactively apply one rule. Dry-run by default.

    New rules do NOT silently rewrite history — this is the only path, and the
    preview's counts equal the commit's results (decision 35's
    preview-exactness rule), because both walk the identical candidate list.
    That list is drawn from EVERY live row, walked in keyset batches with no
    cap (#356); ``scanned`` reports how many rows were examined.

    A categorize rule never selects a transfer (#311): a transfer cannot take
    a category, so it is left out of that list — and so out of both runs'
    counts — and reported as ``skipped_transfers``. A split row (decision 87)
    is read-only and left out the same way, reported as ``skipped_splits``.

    ``transfer_link`` needs no apply-history: ``auto_link_pass`` already
    sweeps all live one-sided posted history nightly, so activating the rule
    IS its retroactive application. That asymmetry is the immutability rule
    restated — linking is additive, lineage-preserving and reversible, while
    recategorizing posted rows is a correction, and corrections stay explicit.
    """
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise RuleError("rule not found")
    if rule.type == TYPE_TRANSFER_LINK:
        raise RuleError(
            "transfer_link rules are retroactive via the nightly sweep; "
            "activate the rule instead of applying history"
        )
    if posted not in {"skip", "correct"}:
        raise RuleError("posted must be 'skip' or 'correct'")

    loaded = LoadedRule(
        id=rule.id,
        type=rule.type,
        priority=rule.priority,
        predicate=_predicate_of(rule),
        category_id=rule.category_id,
        payee_name=rule.payee_name,
        pair=None,
        amount_exact=rule.amount_exact,
        effective_from=rule.effective_from,
    )

    from purser import categorize as categorize_service
    from purser import transfers as transfers_service

    # Selection finishes before anything is written. A posted correction
    # mints a replacement on the same date, which could sort behind the
    # cursor and be walked again; selecting first keeps the commit's
    # candidates exactly the dry run's.
    candidates: list[Transaction] = []
    automation: set[uuid.UUID] = set()
    scanned = 0
    skipped_transfers = 0
    skipped_splits = 0
    skipped_currency = 0
    for batch in _live_row_batches(session):
        scanned += len(batch)
        selected: list[Transaction] = []
        for tx in batch:
            candidate = _candidate_of(tx)
            if candidate is None or not core_rules.matches(loaded.predicate, candidate):
                continue
            if rule.type == TYPE_CATEGORIZE and transfers_service.is_transfer(tx):
                # A transfer cannot take a category (decision 62's shapes): a
                # plain one has no category leg, and a split loan payment's one
                # category leg is its interest. Applying the rule would fail on it
                # (or, for a real-account target, re-point the transfer), and the
                # automation-categorized test can admit one — an [account]
                # conversion in place is audited as transaction.categorize.
                # Skipped before selection, so the dry run is the commit (#311).
                skipped_transfers += 1
                continue
            if rule.type == TYPE_CATEGORIZE and transfers_service.split_lines(tx):
                # A split row is read-only (decision 87): its category legs are
                # lines, not the row's category, and _flat_legs refuses it.
                skipped_splits += 1
                continue
            if (
                rule.type == TYPE_CATEGORIZE
                and rule.category_id is not None
                and not categorize_service.fits_currency(session, tx, rule.category_id)
            ):
                # v1 is single-currency per transaction: the category would
                # be refused on this row (#361). Skipped before selection, so
                # the dry run is the commit.
                skipped_currency += 1
                continue
            selected.append(tx)
        if rule.type == TYPE_CATEGORIZE:
            # Per batch, so the IN list stays one batch long.
            automation |= _automation_categorized(session, [t.id for t in selected])
        candidates.extend(selected)

    result: dict[str, Any] = {
        "rule_id": str(rule.id),
        "type": rule.type,
        "dry_run": dry_run,
        "scanned": scanned,
        "matched": len(candidates),
        "pending": 0,
        "posted": 0,
        "skipped_user_categorized": 0,
        "skipped_transfers": skipped_transfers,
        "skipped_splits": skipped_splits,
        "skipped_currency": skipped_currency,
        "payees_aliased": 0,
    }

    if rule.type == TYPE_CATEGORIZE:
        from purser.categorize import UNCATEGORIZED_PATHS

        for tx in candidates:
            virtual = next((p for p in tx.postings if p.account.is_virtual), None)
            uncategorized = virtual is not None and virtual.account.path in UNCATEGORIZED_PATHS
            if not uncategorized and tx.id not in automation and not include_user_categorized:
                result["skipped_user_categorized"] += 1
                continue
            if tx.status == "posted" and posted == "skip":
                continue
            result["posted" if tx.status == "posted" else "pending"] += 1
            if dry_run or rule.category_id is None:
                continue
            categorize_service._apply_category(
                session,
                tx,
                rule.category_id,
                provenance={"source": "rule", "rule_id": str(rule.id), "stage": "apply_history"},
                actor_token_id=token.id if token else None,
                token=token,
            )
    elif rule.type == TYPE_RENAME and rule.payee_name:
        # Retroactive rename ALIASES the payee entity; no transaction is
        # touched, so the posted guard is never engaged and no reversal pair
        # pollutes the register (decision 46, Q1).
        from purser import ledger as ledger_service

        sources = {tx.payee_id for tx in candidates if tx.payee_id is not None}
        target = ledger_service.resolve_payee(session, rule.payee_name)
        sources.discard(target.id)
        result["payees_aliased"] = len(sources)
        result["pending"] = len(candidates)
        if not dry_run:
            for source_id in sources:
                alias_payee(session, source_id=source_id, target_id=target.id, token=token)

    if not dry_run:
        mark_applied(session, [rule.id])
        audit.record(
            session,
            actor_token_id=token.id if token else None,
            action="rule.apply_history",
            entity_type="rule",
            entity_id=rule.id,
            after=dict(result),
        )
    return result
