"""Write tools: the agent acts with the user's authority (#436).

Every tool here declares the ``write`` scope; a write is attributed in the
audit log and reversible, and proposing instead is the caller's choice per
call. Registered on import.
"""

import uuid
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.mcp._registry import _amount, _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import NOTHING, Emits
from purser.models import Schedule, Suggestion


@audited_tool(kind="write", entity="transaction", scope="write", emits=NOTHING)
async def create_transaction(
    account: str,
    category: str,
    amount: str,
    date_: str,
    payee: str | None = None,
    description: str | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    """Create a transaction in the flat form, with the requested status
    (pending = the bank has not posted it yet; posted is immutable after)."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser.core import ledger as core

        principal = _principal()
        tx = ledger_service.create_flat_transaction(
            session,
            account_ref=account,
            category_ref=category,
            amount=Decimal(amount),
            date=date_type.fromisoformat(date_),
            payee_name=payee,
            description=description,
            requested_status=core.Status(status),
            token=_token_row(session, principal),
        )
        return {"id": str(tx.id), "status": tx.status}

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=NOTHING)
async def update_transaction(
    transaction_id: str,
    amount: str | None = None,
    category: str | None = None,
    payee: str | None = None,
    description: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Edit a transaction. pending/expected edit in place; posted becomes an
    atomic reversal+replacement pair.

    Transfers ride the category field: category='[account path]' converts the
    row into a transfer to that account (absorbing or merging with the bank
    row on the other side when one exists). To turn a transfer
    back into a categorized row, send a plain category plus account='the side
    that keeps the row'. Conversions must be the only change in the call.
    An amount on an expected/pending transfer is the leg of account (a
    schedule instance defaults to its schedule's account), keeps that leg's
    sign and moves both legs; a posted or bank-linked transfer is refused,
    and the error says how to correct it. On a schedule instance an amount,
    date or category edit pins it (the rule stops re-estimating it; undo
    with reset_instance); a description or payee edit is a label and does
    not."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service

        principal = _principal()
        tx = ledger_service.update_transaction(
            session,
            tx_id=uuid.UUID(transaction_id),
            token=_token_row(session, principal),
            amount=Decimal(amount) if amount else None,
            category_ref=category,
            payee_name=payee,
            description=description,
            account_ref=account,
        )
        return {"id": str(tx.id), "status": tx.status}

    return await _run(op)


@audited_tool(kind="write", entity="schedule", scope="write", emits=NOTHING)
async def create_schedule(
    name: str,
    account: str,
    rrule: str,
    amount: str | None = None,
    amount_type: str = "fixed",
    payee: str | None = None,
    category: str | None = None,
    dtstart: str | None = None,
    estimate_basis: str | None = None,
    autopost_policy: str = "wait_for_match",
    counterparty_account: str | None = None,
    note: str | None = None,
    propose: bool = False,
) -> dict[str, Any]:
    """Create a bill/income schedule (e.g. from a parsed statement email).
    It is created active. When you are acting on your own initiative rather
    than on the user's request (a pattern you noticed), pass propose=true:
    it lands as a suggestion for the user to decide, and is refused when the
    account already has a schedule for that payee or counterparty (the error
    names it). note (200 characters or fewer) is a proposal's evidence for
    the person deciding it, such as the statement line you read; it shows on
    the suggestion's card. A direct create keeps no note.

    rrule is RFC 5545 (FREQ=MONTHLY;BYMONTHDAY=6,20 is semi-monthly); dtstart
    (YYYY-MM-DD) anchors it — pass the last real occurrence so anchor-sensitive
    rules count from it. amount is the schedule account's leg (negative =
    money out); required for amount_type=fixed, the fallback for
    amount_type=estimated (estimate_basis last | rolling_avg_3 |
    rolling_avg_12 from confirmed matches, or counterparty_balance = pay the
    counterparty's full reported balance, for card payments).

    A recurring TRANSFER — card payment, loan payment, 529/IRA contribution —
    names the other real account as counterparty_account instead of a
    category; its instances are two-leg expected transfers. The schedule is
    itself the signal that links its bank rows (the same two accounts, exactly
    the instance amount, both sides within 3 days of the instance — also when
    the sides post on different days), and the merged transfer settles the
    instance once."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        schedule = service.create_schedule(
            session,
            name=name,
            account_ref=account,
            rrule=rrule,
            amount_type=amount_type,
            amount=Decimal(amount) if amount is not None else None,
            payee_name=payee,
            category_path=category,
            estimate_basis=estimate_basis,
            autopost_policy=autopost_policy,
            dtstart=date_type.fromisoformat(dtstart) if dtstart else None,
            counterparty_account_ref=counterparty_account,
            source="agent",
            token=_token_row(session, principal),
            note=note,
            propose=propose,
        )
        if isinstance(schedule, Suggestion):
            # propose=true: the draft landed in the inbox (#233).
            return {
                "id": str(schedule.id),
                "status": "proposed",
                "suggestion_id": str(schedule.id),
                "next_due": None,
            }
        return {
            "id": str(schedule.id),
            "status": schedule.status,
            "next_due": schedule.next_due.isoformat() if schedule.next_due else None,
        }

    return await _run(op)


@audited_tool(kind="write", entity="schedule", scope="write", emits=NOTHING)
async def update_schedule(
    schedule_id: str,
    name: str | None = None,
    rrule: str | None = None,
    dtstart: str | None = None,
    amount: str | None = None,
    amount_type: str | None = None,
    estimate_basis: str | None = None,
    payee: str | None = None,
    category: str | None = None,
    autopost_policy: str | None = None,
    counterparty_account: str | None = None,
) -> dict[str, Any]:
    """Edit a schedule's rule (write scope); only the fields sent change.
    Live instances the user has not overridden follow the new rule (slots the
    rule no longer produces are dropped, amounts re-estimated, missing slots
    added); overridden ("pinned") instances keep their amounts and dates.
    A new name or payee relabels every live instance, pinned ones too, whose
    description or payee still equals the old one (`relabeled` counts them);
    labels set by hand stay. category and counterparty_account are mutually
    exclusive: sending one switches the schedule's shape between flat and
    transfer."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        updated = service.update_schedule(
            session,
            schedule_id=uuid.UUID(schedule_id),
            token=_token_row(session, principal),
            name=name,
            rrule=rrule,
            dtstart=date_type.fromisoformat(dtstart) if dtstart else None,
            amount_type=amount_type,
            amount=Decimal(amount) if amount is not None else None,
            estimate_basis=estimate_basis,
            category_path=category,
            payee_name=payee,
            autopost_policy=autopost_policy,
            counterparty_account_ref=counterparty_account,
        )
        schedule = updated.schedule
        return {
            "id": str(schedule.id),
            "status": schedule.status,
            "rrule": schedule.rrule,
            "dtstart": schedule.dtstart.isoformat(),
            "next_due": schedule.next_due.isoformat() if schedule.next_due else None,
            "instances": len(service.list_instances(session, schedule)),
            "relabeled": updated.relabeled,
        }

    return await _run(op)


@audited_tool(kind="write", entity="schedule", scope="write", emits=NOTHING)
async def end_schedule(schedule_id: str, action: str = "end") -> dict[str, Any]:
    """Pause, resume or end a schedule (write scope). action=pause stops
    expecting it (live unedited instances are dropped); action=resume makes a
    paused schedule active again and brings its instances back; action=end is
    terminal — history and matched instances stay, and an ended schedule
    cannot be resumed. A schedule waiting to be confirmed is a suggestion,
    not a schedule: decide it with decide_suggestion."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        token = _token_row(session, principal)
        if action == "pause":
            schedule = service.pause_schedule(
                session, schedule_id=uuid.UUID(schedule_id), token=token
            )
        elif action == "resume":
            schedule = service.resume_schedule(
                session, schedule_id=uuid.UUID(schedule_id), token=token
            )
        elif action == "end":
            schedule = service.end_schedule(
                session, schedule_id=uuid.UUID(schedule_id), token=token
            )
        else:
            raise ValueError("action must be pause | resume | end")
        return {
            "id": str(schedule.id),
            "status": schedule.status,
            "next_due": schedule.next_due.isoformat() if schedule.next_due else None,
        }

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=NOTHING)
async def skip_instance(transaction_id: str) -> dict[str, Any]:
    """Skip one occurrence of a schedule (write scope): the expected instance
    is voided and its slot stays filled, so it does not come back — use it for
    "not this month". The schedule itself is untouched."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser.core import ledger as core

        principal = _principal()
        tx = ledger_service.get_transaction(session, uuid.UUID(transaction_id))
        if tx.schedule_id is None or tx.status != core.Status.EXPECTED.value:
            raise ValueError("not a live schedule instance")
        tx = ledger_service.transition_status(
            session,
            tx_id=tx.id,
            new_status=core.Status.VOID,
            token=_token_row(session, principal),
        )
        return {"transaction_id": str(tx.id), "status": tx.status}

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=Emits(amount=("amount",)))
async def override_instance(
    transaction_id: str, amount: str | None = None, date: str | None = None
) -> dict[str, Any]:
    """Override one occurrence of a schedule: a different amount ("this Citi
    payment is 8,067.37") and/or date ("it lands on the 3rd this month").
    The instance becomes pinned — the rule's estimate refresh and edits never
    touch it again. date is YYYY-MM-DD. amount is the schedule account's leg,
    in the schedule's sign (a card payment from checking: -8067.37); on a
    transfer schedule both legs move. A credit card's statement balance
    goes to record_statement_balance instead: it addresses the payment by
    card and due date and keeps the statement on the instance."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser.core import ledger as core

        principal = _principal()
        tx = ledger_service.get_transaction(session, uuid.UUID(transaction_id))
        if tx.schedule_id is None or tx.status != core.Status.EXPECTED.value:
            raise ValueError("not a live schedule instance")
        tx = ledger_service.update_transaction(
            session,
            tx_id=tx.id,
            token=_token_row(session, principal),
            amount=Decimal(amount) if amount is not None else None,
            date=date_type.fromisoformat(date) if date else None,
        )
        schedule = session.get(Schedule, tx.schedule_id)
        leg = next(
            (
                p
                for p in tx.postings
                if schedule is not None and p.account_id == schedule.account_id
            ),
            None,
        )
        return {
            "transaction_id": str(tx.id),
            "date": tx.date.isoformat(),
            "amount": str(leg.amount) if leg is not None else None,
            "pinned": tx.schedule_pinned_at is not None,
        }

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=Emits(amount=("amount",)))
async def reset_instance(transaction_id: str) -> dict[str, Any]:
    """Undo an override (write scope): the instance is unpinned and gets
    back the rule's date (its slot), name, payee and current estimate, then
    follows the rule again. If the rule no longer has that slot, or
    estimates nothing, the instance is dropped instead (status void)."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        tx = service.reset_instance(
            session, tx_id=uuid.UUID(transaction_id), token=_token_row(session, principal)
        )
        schedule = session.get(Schedule, tx.schedule_id)
        leg = next(
            (
                p
                for p in tx.postings
                if schedule is not None and p.account_id == schedule.account_id
            ),
            None,
        )
        return {
            "transaction_id": str(tx.id),
            "status": tx.status,
            "date": tx.date.isoformat(),
            "amount": str(leg.amount) if leg is not None else None,
            "pinned": tx.schedule_pinned_at is not None,
        }

    return await _run(op)


@audited_tool(
    kind="write",
    entity="transaction",
    scope="write",
    emits=Emits(narrative=True, amount=("amount", "previous_amount")),
)
async def record_statement_balance(
    statement_balance: str, account: str, due_date: str, statement_date: str | None = None
) -> dict[str, Any]:
    """Record a credit card's statement balance against the payment it is
    due on (write scope). Use it for a card statement email or notice
    ("your Amex Platinum statement balance is $8,347.00, payment due Oct
    19"): account is the card (path or id), statement_balance the figure as
    printed (the amount owed, positive), due_date the payment due date
    (YYYY-MM-DD), statement_date the closing date if the statement gives one.
    One call: the card's live payment instance due nearest due_date (within
    14 days) takes the statement balance on both legs and is pinned, so the
    card's current balance, which card-payment schedules estimate from by
    default, no longer moves it. forecast_balance shows that event with
    basis 'statement'. Recording again replaces the figure; reset_instance
    hands the payment back to the current-balance estimate. Credit cards
    only: for a loan payment or any other bill, use override_instance."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        try:
            figure = Decimal(statement_balance)
        except InvalidOperation as exc:
            raise ValueError("statement_balance must be a decimal string") from exc
        recorded = service.record_statement_balance(
            session,
            token=_token_row(session, principal),
            statement_balance=figure,
            statement_date=date_type.fromisoformat(statement_date) if statement_date else None,
            account_ref=account,
            due_date=date_type.fromisoformat(due_date),
        )
        tx, row = recorded.tx, recorded.statement
        return {
            "transaction_id": str(tx.id),
            "schedule": recorded.schedule.name,
            "date": tx.date.isoformat(),
            "amount": _amount(recorded.amount),
            "previous_amount": _amount(recorded.previous_amount),
            "statement_balance": _amount(row.statement_balance),
            "statement_date": row.statement_date.isoformat() if row.statement_date else None,
            "basis": service.instance_basis(recorded.schedule, tx, row),
            "pinned": tx.schedule_pinned_at is not None,
        }

    return await _run(op)


@audited_tool(kind="write", entity="match", scope="write", emits=Emits(amount=("variance",)))
async def match(expected_id: str, posted_id: str) -> dict[str, Any]:
    """Manually match an expected transaction to a posted one (write scope)
    — "mark paid". Open proposals also wait in list_suggestions
    (kind='match'), decided with decide_suggestion; calling match with
    expected_id and proposed_posted_id (from list_unmatched) confirms the
    proposal in place too, and can pair a bank row a rejected proposal
    named. A posted row proposed for a different expected is refused,
    naming it."""

    def op(session: Session) -> dict[str, Any]:
        from purser import matching as service

        principal = _principal()
        m = service.manual_match(
            session,
            expected_id=uuid.UUID(expected_id),
            posted_id=uuid.UUID(posted_id),
            token=_token_row(session, principal),
        )
        return {"match_id": str(m.id), "state": m.state, "variance": _amount(m.variance)}

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=NOTHING)
async def confirm_transfer(
    outgoing_id: str, incoming_id: str, remainder_category: str | None = None
) -> dict[str, Any]:
    """Merge an opposite-amount pair into one transfer (write scope).
    Posted cross-account pairs within 3 days that carry a transfer signal
    (a transfer keyword, the counterparty account's name, a transfer_link
    rule, or an instance of an active transfer schedule) auto-link at import
    and in the nightly sweep; pairs WITHOUT a signal stay candidates and need
    this explicit confirm. A row already matched to an expected transfer
    still pairs with that transfer's other side, and the match moves onto
    the merged transfer.

    Split loan payment: when the cash leg pays more than the principal leg
    the loan account shows (the rest is interest or fees), pass
    remainder_category = the expense category for that part (e.g.
    expenses.fees.interest). Both bank legs keep their exact amounts, the
    difference becomes one posting to that category, and no full-payment
    loan leg is invented. The incoming (principal) amount must be positive
    and smaller than the outgoing (cash) amount; the result then also
    carries remainder and remainder_category. Never automatic; unlinking
    the transfer restores both bank rows.

    An ordinary confirm also proposes a transfer_link rule for this account
    pair, scoped to the payee, in the suggestions inbox (suggestion_id in the
    result; repeat confirms refresh the same one). It links nothing until a
    human accepts it with decide_suggestion and confirms its projected link
    count when activating the rule; reject it to never see it again."""

    def op(session: Session) -> dict[str, Any]:
        from purser import matching as service

        principal = _principal()
        merged, suggestion = service.confirm_transfer_and_propose(
            session,
            outgoing_id=uuid.UUID(outgoing_id),
            incoming_id=uuid.UUID(incoming_id),
            token=_token_row(session, principal),
            remainder_category_ref=remainder_category,
        )
        return service.transfer_confirmation(merged, suggestion)

    return await _run(op)


@audited_tool(kind="write", entity="account", scope="write", emits=NOTHING)
async def update_account(
    account: str,
    name: str | None = None,
    description: str | None = None,
    hidden: bool | None = None,
    min_balance: str | None = None,
) -> dict[str, Any]:
    """Rename, re-describe or hide a real account, or set its floor (write
    scope); only the fields sent change. name is the display name only: the
    account's path, which tools, rules and outside integrations use to find
    it, never changes, so a rename breaks nothing that refers to the account. The name is also
    what transfer auto-link looks for in bank text (a row that mentions the
    counterparty account's name is a transfer signal), so a rename changes
    which bank descriptions link to the account. description="" clears it.
    hidden=true hides the account in the app's account list only: it still
    syncs, counts in net worth, and is listed by list_accounts with
    hidden=true. min_balance is the account's floor, a decimal string in
    ledger sign: use it when the user says "warn me if checking goes under
    $500" (min_balance="500.00"). Each night Purser projects the next 30 days
    and emits a forecast.shortfall event when the projection drops below the
    floor; forecast_balance shows it as min_balance. A credit card is
    negative when owed, so "warn me past $4,500 owed" is "-4500.00".
    min_balance="" clears the floor and stops the warnings. Checking,
    savings, cash and credit-card accounts only. Categories are renamed in
    the app, not here, and moving an account to another path is done in the
    app too."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service

        principal = _principal()
        if name is None and description is None and hidden is None and min_balance is None:
            raise ValueError("nothing to change: pass name, description, hidden or min_balance")
        if name is not None and not name.strip():
            raise ValueError("name must not be empty")
        floor: Decimal | None = None
        if min_balance is not None and min_balance.strip():
            try:
                floor = Decimal(min_balance.strip())
            except InvalidOperation as exc:
                raise ValueError(
                    'min_balance must be a decimal string such as "500.00", or "" to clear it'
                ) from exc
        target = ledger_service.resolve_account(session, account)
        if target.is_virtual:
            raise ledger_service.LedgerError(
                f"{target.path} is a category; categories are renamed in the app"
            )
        updated = ledger_service.update_account(
            session,
            account_id=target.id,
            name=name.strip() if name is not None else None,
            description=description,
            hidden=hidden,
            min_balance=floor,
            min_balance_set=min_balance is not None,
            actor_token_id=principal.token_id,
        )
        return {
            "id": str(updated.id),
            "path": updated.path,
            "name": updated.name,
            "hidden": updated.hidden_at is not None,
            "min_balance": _amount(updated.min_balance),
        }

    return await _run(op)


@audited_tool(kind="write", entity="account", scope="write", emits=NOTHING)
async def reconcile_account(
    account: str,
    as_of: str,
    statement_balance: str,
    basis: str = "posted",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reconcile a checking, savings, cash or credit-card account to a
    statement (write scope). statement_balance is the figure AS THE STATEMENT
    SHOWS IT: positive = money in the account, or for a credit card the amount
    owed. Purser converts it to ledger sign (cards are negative when owed), and
    every other amount in the answer is ledger sign. basis is posted (default)
    or pending_inclusive (posted plus pending); as_of is YYYY-MM-DD, today or
    earlier. Purser posts ONE adjustment for the difference to a system
    category, and it lands in the review set; when the ledger already matches
    nothing is posted and the clean reconcile is still recorded. dry_run=true
    answers the same without writing. Retrying the same date and balance
    returns created=false while it still holds; once a backdated change has
    moved the balance on that date it fails with the difference.
    list_accounts shows whether each account's latest reconciliation still
    holds."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser import reconciliation as service

        principal = _principal()
        try:
            figure = Decimal(statement_balance)
        except InvalidOperation as exc:
            raise ValueError("statement_balance must be a decimal string") from exc
        outcome = service.reconcile(
            session,
            account_id=ledger_service.resolve_account(session, account).id,
            as_of=date_type.fromisoformat(as_of),
            statement_balance=figure,
            basis=basis,
            dry_run=dry_run,
            token=_token_row(session, principal),
        )
        return {
            "reconciliation_id": (
                str(outcome.reconciliation_id) if outcome.reconciliation_id else None
            ),
            "account_id": str(outcome.account_id),
            "as_of": outcome.as_of.isoformat(),
            "basis": outcome.basis,
            "statement_balance": _amount(outcome.statement_balance),
            "target_balance": _amount(outcome.target_balance),
            "ledger_balance": _amount(outcome.ledger_balance),
            "adjustment": _amount(outcome.adjustment),
            "adjustment_tx_id": (
                str(outcome.adjustment_tx_id) if outcome.adjustment_tx_id else None
            ),
            "category": outcome.category_path,
            "reconciled_through": (
                outcome.reconciled_through.isoformat() if outcome.reconciled_through else None
            ),
            "created": outcome.created,
            "dry_run": outcome.dry_run,
        }

    return await _run(op)


@audited_tool(kind="write", entity="valuation", scope="write", emits=NOTHING)
async def record_valuation(
    account: str,
    as_of: str,
    value: str,
    convention: str = "ledger",
) -> dict[str, Any]:
    """Record a loan or property account's value on a date (write scope).
    Such an account's balance IS its latest valuation, in net worth and on
    every account surface, so this is how it is kept current: it cannot be
    reconciled (reconcile_account is for checking, savings, cash and credit
    cards). as_of is YYYY-MM-DD. value is a decimal string in ledger sign by
    default (convention='ledger'): a property positive, a loan NEGATIVE while
    money is owed. To enter the figure as the statement prints it, a loan's
    amount owed positive, pass convention='statement' and Purser negates it,
    exactly as reconcile_account converts a statement balance. Never flip the
    sign yourself: a positive loan value in ledger convention is refused,
    never recorded as an asset. The answer's value is the stored figure, in
    ledger sign."""

    def op(session: Session) -> dict[str, Any]:
        from purser import networth as service
        from purser.core import ledger as core
        from purser.models import Account

        principal = _principal()
        try:
            figure = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("value must be a decimal string") from exc
        valuation = service.record_valuation(
            session,
            account_ref=account,
            as_of=date_type.fromisoformat(as_of),
            value=figure,
            convention=convention,
            actor_token_id=principal.token_id,
        )
        valued = session.get(Account, valuation.account_id)
        assert valued is not None
        return {
            "id": str(valuation.id),
            "account": valued.path,
            "as_of": valuation.as_of.isoformat(),
            "value": _amount(core.quantize(valuation.value)),
            "source": valuation.source,
        }

    return await _run(op)


@audited_tool(kind="write", entity="connection", scope="write", emits=NOTHING)
async def trigger_sync(connection_id: str | None = None) -> dict[str, Any]:
    """Fetch from SimpleFIN now (archive-first, then import). Omit
    connection_id to sync every active connection."""

    def op(session: Session) -> dict[str, Any]:
        from purser.feed import service as feed_service
        from purser.models import Connection

        principal = _principal()
        if connection_id is not None:
            ids = [uuid.UUID(connection_id)]
        else:
            ids = list(
                session.execute(
                    select(Connection.id).where(Connection.status != "disabled")
                ).scalars()
            )
        results = []
        for cid in ids:
            try:
                stats = feed_service.sync_connection(
                    session, connection_id=cid, actor_token_id=principal.token_id
                )
                results.append(
                    {"connection_id": str(cid), "imported": stats["imported"], "ok": True}
                )
            except feed_service.FeedError as exc:
                results.append({"connection_id": str(cid), "ok": False, "error": str(exc)})
        return {"synced": results}

    return await _run(op)
