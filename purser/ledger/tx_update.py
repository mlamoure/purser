"""Transaction edits: in-place updates, reversal + replacement corrections,
machine recategorization and the status funnel (DESIGN §4)."""

import uuid
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import audit, balances, events
from purser.admin import security
from purser.core import ledger as core
from purser.ledger.accounts import _assert_accounts_open, resolve_account
from purser.ledger.errors import LedgerError
from purser.ledger.payees import resolve_payee
from purser.ledger.tx_create import (
    _actor_origin,
    _persist_transaction,
    _tx_audit_shape,
    check_category_currency,
)
from purser.ledger.tx_read import get_transaction
from purser.models import Account, ApiToken, Posting, Transaction

# ---------------------------------------------------------------------------
# Transactions — update / lifecycle
# ---------------------------------------------------------------------------


def update_transaction(
    session: Session,
    *,
    tx_id: uuid.UUID,
    token: ApiToken,
    date: date_type | None = None,
    amount: Decimal | None = None,
    category_ref: str | None = None,
    payee_name: str | None = None,
    description: str | None = None,
    account_ref: str | None = None,
) -> Transaction:
    """Edit semantics by status (DESIGN §4):

    - expected/pending: freely editable in place; on a transfer ``amount``
      is the leg of the side ``account_ref`` names (a schedule instance
      defaults to its schedule account) and both legs move, direction fixed
      (``transfers.set_amount_in_place``, decision 78)
    - posted: atomic reversal + replacement pair linked via reverses_id;
      requires write scope (immutability is not proposable-around); a
      posted transfer is refused — unlink or convert it back instead
    - a schedule instance is pinned by an amount, date or category edit;
      description and payee are labels and leave it unpinned (decision 79)

    Category carries the transfer sugar (DESIGN §3.3): '[account]' converts
    the row to a transfer to that account; a plain category on a
    transfer-shaped row converts it back (``account_ref`` names which side
    keeps the row). Conversions are their own operation — no other field
    may change in the same call.
    """
    from purser import transfers  # function-local: transfers imports purser.ledger

    tx = get_transaction(session, tx_id)
    if tx.status == core.Status.VOID:
        raise LedgerError("void transactions are immutable lineage")
    # A closed account's rows are read-only (in-place edits and reversal-based
    # corrections alike): reopen the account to correct history (DESIGN §3.4).
    _assert_accounts_open(session, {p.account_id for p in tx.postings})

    if category_ref is not None:
        other_edits = (date, amount, payee_name, description)
        target_ref = core.parse_transfer_target(category_ref)
        if target_ref is not None:
            if any(v is not None for v in other_edits):
                raise LedgerError("convert to a transfer separately from other edits")
            return transfers.convert_to_transfer(session, tx=tx, target_ref=target_ref, token=token)
        if transfers.is_transfer(tx):
            if any(v is not None for v in other_edits):
                raise LedgerError("recategorize a transfer separately from other edits")
            return transfers.convert_to_category(
                session,
                tx=tx,
                category_ref=category_ref,
                keep_account_ref=account_ref,
                token=token,
            )
    if tx.status == core.Status.POSTED:
        if not security.can_write(token.scopes):
            raise LedgerError("editing a posted transaction requires write scope")
        # A posted row is corrected at most once; the correction chain continues
        # from the replacement. Backstopped by transactions_reverses_id_uniq.
        correction = session.execute(
            select(Transaction.id).where(Transaction.reverses_id == tx.id)
        ).scalar_one_or_none()
        if correction is not None:
            raise LedgerError("transaction already corrected; edit the replacement instead")
        if transfers.is_transfer(tx):
            raise LedgerError(transfers.posted_edit_refusal(session, tx))
        return _reverse_and_replace(
            session,
            tx=tx,
            token=token,
            date=date,
            amount=amount,
            category_ref=category_ref,
            payee_name=payee_name,
            description=description,
        )

    before = _tx_audit_shape(tx, _specs_of(tx))
    old_date = tx.date
    # Postings first: they validate before anything on the row changes.
    if amount is not None or category_ref is not None:
        if transfers.is_transfer(tx):
            # A category on a transfer returned above, so this is an amount.
            assert amount is not None
            transfers.set_amount_in_place(session, tx=tx, amount=amount, account_ref=account_ref)
        else:
            real, category = _flat_legs(tx)
            new_amount = core.quantize(amount if amount is not None else real.amount)
            if new_amount == 0:
                raise LedgerError("amount must be nonzero")
            new_category = (
                resolve_account(session, category_ref) if category_ref else category.account
            )
            if not new_category.is_virtual:
                raise LedgerError(f"{new_category.path} is not a category")
            if category_ref:
                check_category_currency(real.currency, new_category)
            real.amount = new_amount
            category.amount = -new_amount
            # The relationship, not the FK: the session keeps loaded objects
            # (expire_on_commit=False), so the response would otherwise read
            # the old category (#383).
            category.account = new_category
    if date is not None:
        tx.date = date
    if description is not None:
        tx.description = description
    if payee_name is not None:
        tx.payee = resolve_payee(session, payee_name)
    session.flush()
    # In-place edits move date/amount on countable rows; expected rows are
    # outside the balance set. min() covers a date moved in either direction.
    if tx.status == core.Status.PENDING.value:
        balances.mark_dirty(session, min(old_date, tx.date))
    # A token-bearing edit is a real, visible decision about the row, so it
    # leaves the unreviewed set (#175): a pending edit is still a human or
    # agent looking.
    from purser import review as review_service
    from purser import schedules as schedules_service

    review_service.mark_if_in_scope(session, tx=tx, token=token, via=review_service.VIA_UPDATE)
    # A token-bearing amount/date/category edit of a schedule instance is the
    # user's override: pin it so the rule's refresh/resync never undoes it
    # (#231). Description and payee are labels, not overrides: they leave the
    # instance on the rule's estimate (decision 79).
    schedules_service.touch_instance(
        session, tx, pin=not (date is None and amount is None and category_ref is None)
    )
    audit.record(
        session,
        actor_token_id=token.id,
        action="transaction.update",
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after=_tx_audit_shape(tx, _specs_of(tx)),
    )
    return tx


def _specs_of(tx: Transaction) -> list[core.PostingSpec]:
    return [
        core.PostingSpec(
            account_id=p.account_id,
            amount=Decimal(p.amount),
            currency=p.currency.strip(),
            memo=p.memo,
        )
        for p in tx.postings
    ]


def _flat_legs(tx: Transaction) -> tuple[Posting, Posting]:
    """(real leg, category leg) of a flat transaction; any other shape is
    refused. ``update_transaction`` sends transfers to ``transfers`` first,
    so the message names what a flat edit needs rather than a transfer. A
    split row (one real leg, several category legs) is read-only in v1
    (decision 87), and the refusal says so."""
    real = [p for p in tx.postings if not p.account.is_virtual]
    virtual = [p for p in tx.postings if p.account.is_virtual]
    if len(real) == 1 and len(virtual) >= 2:
        raise LedgerError(
            f"split rows are read-only: this row books {len(virtual)} category legs "
            "against one real leg, and only a row with one category leg can be edited"
        )
    if len(tx.postings) != 2 or len(real) != 1 or len(virtual) != 1:
        raise LedgerError(
            "only a row with one real leg and one category leg can be edited this way"
        )
    return real[0], virtual[0]


def _reverse_and_replace(
    session: Session,
    *,
    tx: Transaction,
    token: ApiToken,
    date: date_type | None,
    amount: Decimal | None,
    category_ref: str | None,
    payee_name: str | None,
    description: str | None,
) -> Transaction:
    # Validate the replacement BEFORE persisting anything: a failure here must
    # not leave a dangling reversal behind.
    real, category = _flat_legs(tx)
    new_amount = core.quantize(amount if amount is not None else real.amount)
    if new_amount == 0:
        raise LedgerError("amount must be nonzero")
    new_category = resolve_account(session, category_ref) if category_ref else category.account
    if not new_category.is_virtual:
        raise LedgerError(f"{new_category.path} is not a category")
    if category_ref:
        check_category_currency(real.currency, new_category)

    replacement_specs = core.expand_flat(
        account_id=real.account_id,
        category_id=new_category.id,
        amount=new_amount,
        currency=real.currency.strip(),
    )
    return reverse_and_replace_with_specs(
        session,
        tx=tx,
        token=token,
        replacement_specs=replacement_specs,
        date=date,
        payee_name=payee_name,
        description=description,
    )


def reverse_and_replace_with_specs(
    session: Session,
    *,
    tx: Transaction,
    token: ApiToken | None,
    replacement_specs: list[core.PostingSpec],
    date: date_type | None = None,
    payee_name: str | None = None,
    description: str | None = None,
    origin: core.Origin | None = None,
    action: str = "transaction.correct",
    audit_extra: dict[str, Any] | None = None,
) -> Transaction:
    """The correction pair for a posted row, with the replacement's postings
    supplied by the caller — shared by flat recategorization, transfer
    conversion, and machine categorization. ``token=None`` is a system
    actor (scope gates are the caller's responsibility)."""
    original_specs = _specs_of(tx)
    reversal = _persist_transaction(
        session,
        specs=core.reversal_postings(original_specs),
        date=date or tx.date,
        payee_name=None,
        description=f"Reversal of {tx.id}",
        requested_status=core.Status.POSTED,
        origin=core.Origin.SYSTEM,
        token=token,
        reverses_id=tx.id,
    )
    payee = tx.payee.name if tx.payee else None
    replacement = _persist_transaction(
        session,
        specs=replacement_specs,
        date=date or tx.date,
        payee_name=payee_name or payee,
        description=description if description is not None else tx.description,
        requested_status=core.Status.POSTED,
        origin=origin if origin is not None else _actor_origin(token),
        token=token,
        reverses_id=reversal.id,
    )
    # The funnel marked the pair's (possibly NEW) date; a date moved FORWARD
    # still demotes the original at its OLD date, so cover that too.
    balances.mark_dirty(session, tx.date)
    # Correction-chain review inheritance (#175, DESIGN §4): a token-bearing
    # actor's replacement is reviewed (the correction IS the review); a
    # system/rule actor's carries the original's state, so automation neither
    # grants nor revokes it.
    from purser import review as review_service

    review_service.inherit(session, original_id=tx.id, replacement_id=replacement.id, token=token)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action=action,
        entity_type="transaction",
        entity_id=tx.id,
        before=_tx_audit_shape(tx, original_specs),
        after={
            "reversal_id": str(reversal.id),
            "replacement_id": str(replacement.id),
            **_tx_audit_shape(replacement, replacement_specs),
            **(audit_extra or {}),
        },
    )
    return replacement


def recategorize_posted(
    session: Session,
    *,
    tx: Transaction,
    category_id: uuid.UUID,
    token: ApiToken | None,
    provenance: dict[str, Any],
) -> Transaction:
    """Machine categorization of a POSTED row: the standard reversal +
    replacement pair, replacement ``origin=rule`` (it exists for exactly
    this), provenance recorded in the audit row. ``token=None`` is the
    scheduled system pass; the caller enforces scope for token-bearing runs.
    """
    correction = session.execute(
        select(Transaction.id).where(Transaction.reverses_id == tx.id)
    ).scalar_one_or_none()
    if correction is not None:
        raise LedgerError("transaction already corrected; edit the replacement instead")
    real, _category = _flat_legs(tx)
    category = session.get(Account, category_id)
    if category is None:
        raise LedgerError("category not found")
    check_category_currency(real.currency, category)
    specs = core.expand_flat(
        account_id=real.account_id,
        category_id=category_id,
        amount=core.quantize(Decimal(real.amount)),
        currency=real.currency.strip(),
    )
    return reverse_and_replace_with_specs(
        session,
        tx=tx,
        token=token,
        replacement_specs=specs,
        origin=core.Origin.RULE,
        action="transaction.categorize",
        audit_extra=provenance,
    )


def transition_status(
    session: Session, *, tx_id: uuid.UUID, new_status: core.Status, token: ApiToken | None
) -> Transaction:
    """The status funnel (DESIGN §4). ``token=None`` is the system actor —
    the guarded retired-rows purge voids through here (#284) — and may never
    post: posting is a token-bearing decision about a row. Voiding a pending
    that a live match names releases or rescinds that match (decision 69)."""
    tx = get_transaction(session, tx_id)
    _assert_accounts_open(session, {p.account_id for p in tx.postings})
    old = core.Status(tx.status)
    if old == core.Status.POSTED and new_status == core.Status.VOID:
        # Bare voids erase a posted row's balance effect with no lineage.
        # Void-with-link happens inside corrections and transfer merges only.
        raise LedgerError(
            "posted transactions cannot be voided directly; "
            "correct via PATCH or confirm a transfer merge"
        )
    if not core.can_transition(old, new_status):
        raise LedgerError(f"cannot transition {old.value} -> {new_status.value}")
    if new_status == core.Status.POSTED and (token is None or not security.can_write(token.scopes)):
        raise LedgerError("posting a transaction requires write scope")
    # A posted chain head is what a schedule estimate learns from (decision
    # 69): posting a matched pending by hand moves it as the feed posting it
    # would, so bracket the change with the same probe (#330).
    from purser import matching  # function-local: matching imports this module

    probes = (
        matching.estimate_probes_on(session, tx)
        if old == core.Status.PENDING and new_status == core.Status.POSTED
        else []
    )
    # A void row settles nothing (decision 69): voiding a matched pending by
    # hand settles its match the way the feed voiding it does, with no
    # successor (#343). Captured before the change, like the feed's.
    voided_match = (
        matching.live_match_on(session, tx)
        if old == core.Status.PENDING and new_status == core.Status.VOID
        else None
    )
    tx.status = new_status.value
    session.flush()
    # Only membership changes move balances: pending<->posted are both
    # countable, expected/void are not (decision 38).
    countable = {core.Status.PENDING, core.Status.POSTED}
    if (old in countable) != (new_status in countable):
        balances.mark_dirty(session, tx.date)
    if tx.schedule_id is not None:
        # A skipped (voided) instance keeps its occurrence_date, so the slot
        # stays filled and the bill does not come back; the schedule's
        # next_due moves on (#231).
        from purser import schedules as schedules_service

        schedules_service.touch_instance(session, tx, pin=False)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action=f"transaction.{new_status.value}",
        entity_type="transaction",
        entity_id=tx.id,
        before={"status": old.value},
        after={"status": new_status.value},
    )
    if voided_match is not None:
        matching.settle_voided_pending(
            session,
            voided_match,
            successor=None,
            actor_token_id=token.id if token else None,
            reason="pending_voided",
        )
    if new_status == core.Status.POSTED:
        # Posting a row is a token-bearing human/agent decision about it
        # (#175): it leaves the unreviewed set.
        from purser import review as review_service

        review_service.mark_if_in_scope(session, tx=tx, token=token, via=review_service.VIA_POST)
        events.emit(
            session,
            type_="transaction.posted",
            entity_type="transaction",
            entity_id=tx.id,
            payload={"date": tx.date.isoformat(), "description": tx.description},
        )
    matching.emit_estimate_changes(session, probes)
    return tx
