"""Categorization pipeline (DESIGN §11): deterministic memory first, model
only for misses; results auto-apply with full provenance — pending rows in
place, posted rows via reversal+replacement corrections (origin=rule).

The deterministic pass learns from accepted (posted) history only — every
human correction becomes the payee's memory, so corrections improve the hit
rate with no model in the loop. Pending rows never seed it: an unreviewed
AI guess must not launder into "deterministic" for the next transaction.
"""

import uuid
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from purser import ai_providers, appsettings, audit
from purser import memories as memories_service
from purser import rules as rules_service
from purser.core import rules as core_rules
from purser.feed.importer import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
from purser.logging import get_logger
from purser.models import Account, ApiToken, Posting, Transaction

log = get_logger(__name__)

UNCATEGORIZED_PATHS = (UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)
MIN_DETERMINISTIC_OCCURRENCES = 2

# A bracket answer restructures the row and moves a SECOND account's balance,
# where an ordinary category error is one field and one inline edit. Under
# DESIGN §11 rule 5 a bracket conversion may also be reconciled against a live
# counterpart — and a merge is balance-neutral, so §7.3's drift check cannot
# detect a bad one. This bar is what stands in for that missing check, so it
# is deliberately well above the 0.5 default `_parse_reply` assigns when a
# model omits the field. Below it the row stays uncategorized and surfaces
# for review (DESIGN §4).
BRACKET_MIN_CONFIDENCE = 0.85


def payee_memory(session: Session, payee_id: uuid.UUID) -> uuid.UUID | None:
    """The category this payee's accepted history points to: the most common
    non-uncategorized category leg across posted transactions. Posted only —
    pending rows may carry unreviewed AI suggestions."""
    rows = session.execute(
        select(Posting.account_id, func.count().label("n"))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            # Aggregate across the alias group (#174): a retroactive rename
            # points several raw payees at one friendly payee, and memory must
            # count all of them or consolidation buys nothing.
            Transaction.payee_id.in_(rules_service.alias_group(session, payee_id)),
            Transaction.status == "posted",
            Account.is_virtual.is_(True),
            Account.path.not_in(UNCATEGORIZED_PATHS),
        )
        .group_by(Posting.account_id)
        .order_by(func.count().desc())
        .limit(1)
    ).first()
    if rows is None or rows.n < MIN_DETERMINISTIC_OCCURRENCES:
        return None
    account_id: uuid.UUID = rows.account_id
    return account_id


def _uncategorized_rows(session: Session) -> list[Transaction]:
    """Uncategorized pending AND posted rows (chain heads only — a corrected
    row is never corrected twice, and its replacement carries the category).
    Split rows are left out (decision 87): they are read-only, and a category
    leg of one is not the row's category."""
    uncategorized_ids = select(Account.id).where(Account.path.in_(UNCATEGORIZED_PATHS))
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    rows = list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.status.in_(["pending", "posted"]),
                Transaction.superseded_by.is_(None),
                Transaction.id.not_in(reversed_ids),
                Posting.account_id.in_(uncategorized_ids),
            )
            .distinct()
        ).scalars()
    )
    # A closed account is read-only: its posted rows can't take a
    # reversal+replacement correction, so skip them rather than crash the
    # auto-apply pass at the posting chokepoint (DESIGN §3.4).
    from purser import transfers

    return [
        tx
        for tx in rows
        if not any(
            not p.account.is_virtual and p.account.closed_at is not None for p in tx.postings
        )
        and not transfers.split_lines(tx)
    ]


def fits_currency(session: Session, tx: Transaction, category_id: uuid.UUID) -> bool:
    """Whether this row can take this category (or ``[account]`` target).

    v1 is single-currency per transaction (#361): ``_apply_category`` refuses
    a target of another currency, so the automated passes ask first and treat
    such a target as no answer for this row instead of failing the pass on it.
    """
    target = session.get(Account, category_id)
    real = next((p for p in tx.postings if not p.account.is_virtual), None)
    return (
        target is not None and real is not None and real.currency.strip() == target.currency.strip()
    )


def _apply_category(
    session: Session,
    tx: Transaction,
    category_id: uuid.UUID,
    *,
    provenance: dict[str, Any],
    actor_token_id: uuid.UUID | None,
    token: ApiToken | None,
) -> None:
    category = session.get(Account, category_id)
    assert category is not None
    if not category.is_virtual:
        # A REAL account is the `[account]` transfer sugar (DESIGN §3.3): the
        # row becomes a one-sided transfer, not a differently-categorized flat.
        # Delegate — the leg-swap below assumes a virtual leg exists, which a
        # transfer has none of, and `recategorize_posted` builds a flat shape.
        # auto_pairing keeps row *selection* deterministic (§11 rule 5).
        from purser import transfers as transfers_service

        # convert_to_transfer takes the BARE path: `ledger.update_transaction`
        # strips the brackets via core.parse_transfer_target before calling it.
        transfers_service.convert_to_transfer(
            session,
            tx=tx,
            target_ref=category.path,
            token=token,
            auto_pairing=True,
        )
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="transaction.categorize",
            entity_type="transaction",
            entity_id=tx.id,
            after={"category": f"[{category.path}]", **provenance},
        )
        return
    from purser import ledger as ledger_service

    if tx.status == "posted":
        ledger_service.recategorize_posted(
            session, tx=tx, category_id=category_id, token=token, provenance=provenance
        )
        return
    real_leg = next(p for p in tx.postings if not p.account.is_virtual)
    ledger_service.check_category_currency(real_leg.currency, category)
    virtual_leg = next(p for p in tx.postings if p.account.is_virtual)
    before_path = virtual_leg.account.path
    virtual_leg.account = category  # relationship, not FK (expire_on_commit=False)
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="transaction.categorize",
        entity_type="transaction",
        entity_id=tx.id,
        before={"category": before_path},
        after={"category": category.path, **provenance},
    )


def run(
    session: Session,
    *,
    token: ApiToken | None,
    client: httpx.Client | None = None,
) -> dict[str, int]:
    """Categorize uncategorized rows: deterministic pass first, model only
    for what that misses. Fully functional with no provider configured.

    Pending rows are edited in place; POSTED rows get the standard
    reversal+replacement correction (DESIGN §11 — auto-apply, owner decision
    2026-07-11), for the system actor (``token=None``, the scheduled pass)
    and a write principal alike (#436).

    A failing provider never breaks the run: the deterministic results
    survive, the affected transactions count as unresolved, and after the
    first provider failure the remainder of the run skips the provider
    entirely (a down endpoint must not cost one timeout per transaction).
    """
    settings = appsettings.effective()
    stats = {"rule": 0, "deterministic": 0, "ai": 0, "unresolved": 0}
    actor_token_id = token.id if token else None
    # Loaded once per pass, like category_paths below. This is also how rules
    # reach MANUAL and AGENT rows: they deliberately do NOT run inside
    # create_flat_transaction, because a caller who supplied an explicit
    # category has already decided (#174, decision 46).
    active_rules = rules_service.load_active(session)
    category_paths: list[str] | None = None
    category_descriptions: dict[str, str] = {}
    provider_down = False
    for tx in _uncategorized_rows(session):
        real_leg = next((p for p in tx.postings if not p.account.is_virtual), None)
        if active_rules and real_leg is not None:
            rule = rules_service.first_match(
                active_rules,
                rules_service.TYPE_CATEGORIZE,
                core_rules.Candidate(
                    account_id=str(real_leg.account_id),
                    amount=Decimal(real_leg.amount),
                    date=tx.date,
                    payee=tx.payee.name if tx.payee else None,
                    description=tx.description,
                ),
            )
            if (
                rule is not None
                and rule.category_id is not None
                and fits_currency(session, tx, rule.category_id)
            ):
                # Precedence: rule > payee memory > AI (DESIGN §11). A rule
                # must be able to beat a WRONG memory — that is the "I keep
                # correcting this, graduate it into a rule" path. A target of
                # another currency is no answer here, so the next tier tries.
                _apply_category(
                    session,
                    tx,
                    rule.category_id,
                    provenance={"source": "rule", "rule_id": str(rule.id)},
                    actor_token_id=actor_token_id,
                    token=token,
                )
                rules_service.mark_applied(session, [rule.id])
                stats["rule"] += 1
                continue

        if tx.payee_id is not None:
            remembered = payee_memory(session, tx.payee_id)
            if remembered is not None and fits_currency(session, tx, remembered):
                _apply_category(
                    session,
                    tx,
                    remembered,
                    provenance={"source": "deterministic"},
                    actor_token_id=actor_token_id,
                    token=token,
                )
                stats["deterministic"] += 1
                continue
        if settings.ai_provider == "none" or provider_down:
            stats["unresolved"] += 1
            continue
        if category_paths is None:
            options = list(
                session.execute(
                    select(Account).where(
                        Account.is_virtual.is_(True), Account.path.not_in(UNCATEGORIZED_PATHS)
                    )
                ).scalars()
            )
            category_paths = [a.path for a in options]
            category_descriptions = {a.path: a.description for a in options if a.description}
            # Real accounts as `[path]` — the transfer sugar (DESIGN §3.3).
            # Without these a transfer row has NO correct answer available:
            # §3.2 deliberately omits transfer categories because the category
            # FIELD handles them, and this list was virtual-only (issue #183).
            # Hidden and closed accounts are excluded; the row's own account is
            # filtered per-row below (a transfer to itself is not legal).
            transfer_targets = list(
                session.execute(
                    select(Account).where(
                        Account.is_virtual.is_(False),
                        Account.hidden_at.is_(None),
                        Account.closed_at.is_(None),
                    )
                ).scalars()
            )
            bracket_paths = [f"[{a.path}]" for a in transfer_targets]
            category_paths.extend(bracket_paths)
            for a in transfer_targets:
                category_descriptions[f"[{a.path}]"] = (
                    f"transfer to/from the {a.name} account — use when this row "
                    "moves money between the user's own accounts"
                )
        if not category_paths:
            stats["unresolved"] += 1
            continue
        own_bracket = f"[{real_leg.account.path}]" if real_leg else None
        offered = [p for p in category_paths if p != own_bracket]
        try:
            # Relevant memories ride along as prompt context (#176 Q2).
            # ACCOUNT and PAYEE links only: at categorization time the
            # category is the unknown being decided, so a category-linked
            # memory structurally cannot reach this consumer.
            relevant = memories_service.relevant_for(
                session,
                account_ids=[real_leg.account_id] if real_leg else [],
                payee_ids=[tx.payee_id] if tx.payee_id else [],
            )
            suggestion = ai_providers.suggest(
                settings,
                payee=tx.payee.name if tx.payee else None,
                description=tx.description,
                amount=Decimal(real_leg.amount) if real_leg else Decimal(0),
                categories=offered,
                category_descriptions=category_descriptions,
                memories=[m["text"] for m in relevant],
                client=client,
            )
        except ai_providers.AiProviderError as exc:
            provider_down = True
            stats["unresolved"] += 1
            log.warning("categorize.provider_error", error=str(exc))
            continue
        if suggestion is None:
            stats["unresolved"] += 1
            continue
        is_bracket = suggestion.category_path.startswith("[")
        if is_bracket and suggestion.confidence < BRACKET_MIN_CONFIDENCE:
            # Leave it uncategorized rather than restructure the row and move
            # a second account's balance on a low-confidence guess (§11.1).
            stats["unresolved"] += 1
            log.info(
                "categorize.bracket_below_threshold",
                transaction_id=str(tx.id),
                target=suggestion.category_path,
                confidence=suggestion.confidence,
                threshold=BRACKET_MIN_CONFIDENCE,
            )
            continue
        category = session.execute(
            select(Account).where(Account.path == suggestion.category_path.strip("[]"))
        ).scalar_one()
        if not fits_currency(session, tx, category.id):
            stats["unresolved"] += 1
            log.info(
                "categorize.currency_mismatch",
                transaction_id=str(tx.id),
                target=suggestion.category_path,
            )
            continue
        _apply_category(
            session,
            tx,
            category.id,
            provenance={
                "source": "ai",
                "model": suggestion.model,
                "provider": settings.ai_provider,
                "confidence": suggestion.confidence,
                "memory_ids": [m["id"] for m in relevant],
            },
            actor_token_id=actor_token_id,
            token=token,
        )
        stats["ai"] += 1
    return stats
