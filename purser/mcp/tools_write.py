"""Write tools: the agent acts with the user's authority (#436).

Every tool here declares the ``write`` scope; a write is attributed in the
audit log and reversible, and proposing instead is the caller's choice per
call. Registered on import.
"""

import uuid
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.mcp._registry import _amount, _principal, _run, _token_row, audited_tool
from purser.models import Schedule, Suggestion
from purser.privacy import NOTHING, Emits


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
    securities_trade: bool | None = None,
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
    not.

    securities_trade=true (in place of category) marks a row on a brokerage
    account whose holdings come from the feed as a securities trade — a buy
    or sell ("YOU BOUGHT", "Buy", "Reinvestment"…), never spending or
    income; the shares are already in the feed's holdings. Purser never
    guesses which rows are trades. The result's suggestion_id is the rule
    the mark proposes in the inbox (rows like this one, on this account).
    To change a marked row back, send the category it should have. Refused
    on other accounts, transfers, splits, expected rows, and the cash of a
    trade recorded with record_sale (reverse that trade instead)."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser import suggestions as suggestions_service

        principal = _principal()
        token = _token_row(session, principal)
        tx = ledger_service.update_transaction(
            session,
            tx_id=uuid.UUID(transaction_id),
            token=token,
            amount=Decimal(amount) if amount else None,
            category_ref=category,
            payee_name=payee,
            description=description,
            account_ref=account,
            securities_trade=securities_trade,
        )
        out: dict[str, Any] = {"id": str(tx.id), "status": tx.status}
        if securities_trade:
            proposed = suggestions_service.propose_trade_rule(session, tx=tx, token=token)
            out["securities_trade"] = True
            out["suggestion_id"] = str(proposed.id) if proposed is not None else None
        return out

    return await _run(op)


_SPLIT_EMITS = Emits(
    description=("lines[].memo",),
    amount=("amount", "lines[].amount", "lines[].candidates[].amount"),
)


def _split_lines_arg(lines: list[dict[str, Any]]) -> list[Any]:
    from purser import splits
    from purser.ledger import LedgerError

    # The count first, before parsing anything (splits.check_limits bounds
    # the rest once the lines are parsed).
    if len(lines) > splits.MAX_LINES:
        raise LedgerError(f"a split takes at most {splits.MAX_LINES} lines, not {len(lines)}")
    out = []
    for i, line in enumerate(lines):
        if not isinstance(line, dict) or "category" not in line or "amount" not in line:
            raise LedgerError(f"line {i + 1}: give category and amount")
        unknown = set(line) - {"category", "amount", "memo", "backing_ids"}
        if unknown:
            raise LedgerError(f"line {i + 1}: unknown keys {', '.join(sorted(unknown))}")
        raw_backing = line.get("backing_ids") or []
        if not isinstance(raw_backing, list) or len(raw_backing) > splits.MAX_BACKING_ROWS:
            raise LedgerError(
                f"line {i + 1}: backing_ids is a list of at most {splits.MAX_BACKING_ROWS} ids"
            )
        if line.get("memo") is not None and not isinstance(line["memo"], str):
            raise LedgerError(f"line {i + 1}: memo is text")
        try:
            amount = Decimal(str(line["amount"]))
            backing = tuple(uuid.UUID(str(b)) for b in raw_backing)
        except (InvalidOperation, ValueError) as exc:
            raise LedgerError(f"line {i + 1}: {exc}") from exc
        if not amount.is_finite():
            raise LedgerError(f"line {i + 1}: amount must be a finite number")
        out.append(
            splits.LineInput(
                target=str(line["category"]),
                amount=amount,
                memo=line.get("memo"),
                backing_ids=backing,
            )
        )
    return out


def _split_out(session: Session, result: dict[str, Any], tx_id: str) -> dict[str, Any]:
    """The compact result: each line's target, amount and backing; for an
    unbacked [Account] line up to three exact-amount bank rows near the row
    that could back it (pass their ids as backing_ids)."""
    from purser import splits

    lines = []
    for line in result.get("lines", []):
        item: dict[str, Any] = {"category": line["target"], "amount": _amount(line["amount"])}
        if line.get("memo"):
            item["memo"] = line["memo"]
        if line["kind"] == "account":
            item["backing_ids"] = line.get("backing_ids", [])
            if not item["backing_ids"]:
                found = splits.backing_candidates(
                    session,
                    tx_id=uuid.UUID(tx_id),
                    account_ref=line["target"][1:-1],
                    amount=-line["amount"],
                    limit=3,
                )
                exact = [
                    {"id": c["id"], "date": c["date"].isoformat(), "amount": _amount(c["amount"])}
                    for c in found["candidates"]
                    if c["exact"] and not c["backing"]
                ][:3]
                if exact:
                    item["candidates"] = exact
        lines.append(item)
    out: dict[str, Any] = {
        "transaction_id": result["transaction_id"],
        "account": result["account"],
        "amount": _amount(result["amount"]),
    }
    for key in ("dry_run", "replaces_id", "status", "category"):
        if result.get(key) is not None:
            out[key] = result[key]
    if lines:
        out["lines"] = lines
    for key in ("backing_added", "backing_restored", "restored_ids"):
        if result.get(key):
            out[key] = result[key]
    return out


@audited_tool(kind="write", entity="transaction", scope="write", emits=_SPLIT_EMITS)
async def split_transaction(
    transaction_id: str, lines: list[dict[str, Any]], dry_run: bool = False
) -> dict[str, Any]:
    """Split a transaction across categories and accounts, Quicken style, or
    replace the lines of a split (write scope). Each line is
    {category, amount, memo?, backing_ids?}: category is a category path
    (expenses.taxes.federal) or an account in brackets ([assets.retirement_401k])
    for money that went to another account; amount is the line's share of
    the row's amount in the row's sign (negative = money out) and the lines
    must add up to the row's amount exactly — two to 50 lines, none zero,
    never the row's own account; a memo is at most 500 characters. A
    paycheck deposit of 2500.00: gross income.salary 4000.00, taxes
    -900.00, [401k] -600.00 (the 401k account then shows +600.00). An
    account line's backing_ids name that account's own bank rows (summing
    exactly to the line's leg, i.e. its amount negated; at most 50 across
    the lines): they are merged into the split so the money is not counted
    twice; unbacked, the leg is Purser's own. The result lists, for each
    unbacked account line, exact-amount candidates to back it. The row
    keeps the bank's amount; a posted row becomes a reversal + replacement
    (transaction_id in the result is the new row), a pending one you
    entered changes in place, and the bank's pending row is refused until
    it posts. dry_run=true validates and writes nothing. A split is never a
    transfer; unsplit_transaction removes it and restores every backing
    row."""

    def op(session: Session) -> dict[str, Any]:
        from purser import splits

        principal = _principal()
        result = splits.split_transaction(
            session,
            tx_id=uuid.UUID(transaction_id),
            lines=_split_lines_arg(lines),
            token=_token_row(session, principal),
            dry_run=dry_run,
        )
        out = _split_out(session, result, result["transaction_id"])
        if dry_run:
            session.rollback()
        return out

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=Emits(amount=("amount",)))
async def unsplit_transaction(
    transaction_id: str, category: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Remove a split (write scope): the row goes back to a single category —
    category, else the uncategorized placeholder — and every bank row that
    backed an account line comes back exactly as it was (restored_ids). A
    posted split becomes a reversal + replacement (transaction_id is the new
    row). dry_run=true writes nothing."""

    def op(session: Session) -> dict[str, Any]:
        from purser import splits

        principal = _principal()
        result = splits.unsplit_transaction(
            session,
            tx_id=uuid.UUID(transaction_id),
            token=_token_row(session, principal),
            category_ref=category,
            dry_run=dry_run,
        )
        out = _split_out(session, result, result["transaction_id"])
        if dry_run:
            session.rollback()
        return out

    return await _run(op)


@audited_tool(kind="write", entity="schedule", scope="write", emits=NOTHING)
async def create_schedule(
    account: str,
    rrule: str,
    payee: str | None = None,
    memo: str | None = None,
    amount: str | None = None,
    amount_type: str = "fixed",
    date_mode: str = "fixed",
    name: str | None = None,
    category: str | None = None,
    dtstart: str | None = None,
    estimate_basis: str | None = None,
    autopost_policy: str = "wait_for_match",
    counterparty_account: str | None = None,
    note: str | None = None,
    propose: bool = False,
    payment_amount: str | None = None,
    later_estimate: str | None = None,
    later_window: int | None = None,
) -> dict[str, Any]:
    """Create a bill/income schedule (e.g. from a parsed statement email).
    It is created active. When you are acting on your own initiative rather
    than on the user's request (a pattern you noticed), pass propose=true:
    it lands as a suggestion for the user to decide, and is refused when the
    account already has a schedule for that payee or counterparty (the error
    names it). note (200 characters or fewer) is a proposal's evidence for
    the person deciding it, such as the statement line you read; it shows on
    the suggestion's card. A direct create keeps no note.

    payee is who is paid or pays (required for a flat schedule unless memo is
    sent); memo is an optional free-text note. The schedule's label (its
    instances' description) is the memo, else the payee, else a transfer's
    counterparty account. name is the older single field: sent without memo
    it becomes a transfer's memo, a flat schedule's payee when payee is
    omitted, or otherwise its memo — prefer payee and memo.

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
    instance once.

    A payment to a CREDIT CARD that has a statement closing day
    (update_account statement_close_day) can be sized from the statement
    cycle: payment_amount = statement_balance (what the statement showed,
    less credits since), minimum_due (a recorded statement's minimum, else
    amount), fixed (amount, never more than the statement) or
    current_balance; later_estimate sizes the payments after the next =
    statement_average (the average of the last later_window statements,
    default 6), same_cycle_last_year or fixed (amount). An estimated payment
    to a card with a closing day defaults to statement_balance and
    statement_average."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        schedule = service.create_schedule(
            session,
            name=name,
            account_ref=account,
            rrule=rrule,
            date_mode=date_mode,
            amount_type=amount_type,
            amount=Decimal(amount) if amount is not None else None,
            payee_name=payee,
            memo=memo,
            category_path=category,
            estimate_basis=estimate_basis,
            autopost_policy=autopost_policy,
            dtstart=date_type.fromisoformat(dtstart) if dtstart else None,
            counterparty_account_ref=counterparty_account,
            source="agent",
            token=_token_row(session, principal),
            note=note,
            propose=propose,
            payment_amount=payment_amount,
            later_estimate=later_estimate,
            later_window=later_window,
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
    date_mode: str | None = None,
    estimate_basis: str | None = None,
    payee: str | None = None,
    memo: str | None = None,
    category: str | None = None,
    autopost_policy: str | None = None,
    counterparty_account: str | None = None,
    payment_amount: str | None = None,
    later_estimate: str | None = None,
    later_window: int | None = None,
) -> dict[str, Any]:
    """Edit a schedule's rule (write scope); only the fields sent change
    ("" clears payee or memo; a flat schedule keeps at least one).
    Live instances the user has not overridden follow the new rule (slots the
    rule no longer produces are dropped, amounts re-estimated, missing slots
    added); overridden ("pinned") instances keep their amounts and dates.
    A new payee or memo relabels every live instance, pinned ones too, whose
    description or payee still equals the old one (`relabeled` counts them);
    labels set by hand stay. category and counterparty_account are mutually
    exclusive: sending one switches the schedule's shape between flat and
    transfer. payment_amount, later_estimate and later_window size a card
    payment from the statement cycle (see create_schedule);
    payment_amount=off turns that off."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        cycle_off = payment_amount == "off"
        cycle_sent = cycle_off or any(
            v is not None for v in (payment_amount, later_estimate, later_window)
        )
        updated = service.update_schedule(
            session,
            schedule_id=uuid.UUID(schedule_id),
            token=_token_row(session, principal),
            name=name,
            rrule=rrule,
            dtstart=date_type.fromisoformat(dtstart) if dtstart else None,
            date_mode=date_mode,
            amount_type=amount_type,
            amount=Decimal(amount) if amount is not None else None,
            estimate_basis=estimate_basis,
            category_path=category,
            payee_name=payee,
            memo=memo,
            autopost_policy=autopost_policy,
            counterparty_account_ref=counterparty_account,
            payment_amount=None if cycle_off else payment_amount,
            later_estimate=None if cycle_off else later_estimate,
            later_window=None if cycle_off else later_window,
            cycle_set=cycle_sent,
        )
        schedule = updated.schedule
        return {
            "id": str(schedule.id),
            "status": schedule.status,
            "payment_amount": schedule.payment_amount,
            "later_estimate": schedule.later_estimate,
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
    emits=Emits(
        narrative=True,
        amount=("amount", "previous_amount"),
        balance=("statement_balance", "minimum_due"),
    ),
)
async def record_statement_balance(
    statement_balance: str,
    account: str,
    due_date: str,
    statement_date: str | None = None,
    minimum_due: str | None = None,
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
    hands the payment back to the rule's estimate. Credit cards only: for a
    loan payment or any other bill, use override_instance.

    minimum_due is the statement's minimum payment, when it prints one: a
    schedule whose payment_amount is minimum_due pays it, and one that pays
    a fixed sum pays at most the statement. Pass statement_date whenever the
    statement gives it: when its day differs from the card's
    statement_close_day, close_day_hint in the result says what to set with
    update_account; nothing is changed for you. On a card with no closing day
    the date feeds card_statement_cycle's suggestion."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        principal = _principal()
        try:
            figure = Decimal(statement_balance)
        except InvalidOperation as exc:
            raise ValueError("statement_balance must be a decimal string") from exc
        try:
            minimum = Decimal(minimum_due) if minimum_due is not None else None
        except InvalidOperation as exc:
            raise ValueError("minimum_due must be a decimal string") from exc
        recorded = service.record_statement_balance(
            session,
            token=_token_row(session, principal),
            statement_balance=figure,
            statement_date=date_type.fromisoformat(statement_date) if statement_date else None,
            account_ref=account,
            due_date=date_type.fromisoformat(due_date),
            minimum_due=minimum,
        )
        tx, row = recorded.tx, recorded.statement
        extra: dict[str, Any] = {}
        if row.minimum_due is not None:
            extra["minimum_due"] = _amount(row.minimum_due)
        if recorded.close_day_hint is not None:
            extra["close_day_hint"] = recorded.close_day_hint
        return {
            **extra,
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
    this explicit confirm (a recent pair of POSTED rows also waits in
    list_suggestions as a kind='transfer_pair' card, which decide_suggestion
    accepts the same way or rejects as not a transfer; confirming the pair
    here accepts its card — a pair with a pending row has no card). A row
    already matched to an expected transfer still pairs with that transfer's
    other side, and the match moves onto the merged transfer. An id of a row
    corrected since you read it (the categorize pass corrects uncategorized
    posted rows) is followed to its replacement, the same bank line.

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
    asset_kind: str | None = None,
    secured_by: str | None = None,
    revalue_after_days: int | None = None,
    balance_true_up: bool | None = None,
    statement_close_day: int | None = None,
) -> dict[str, Any]:
    """Rename, re-describe or hide a real account, or set its floor (write
    scope); only the fields sent change. asset_kind sets what a hand-valued
    asset (type property) is: house, land, car, boat, rv, jewelry, art,
    equipment or other (a label and an icon only). secured_by, on a loan,
    names the hand-valued asset that secures it (its path or id; "" clears
    it), so the asset reports its equity. revalue_after_days, on an asset,
    is how many days its value may go without an update before Purser calls
    it stale (asset.stale, whats_new.stale_assets); 0 turns that off.
    balance_true_up, on a brokerage account whose holdings come from the
    feed, switches the balance true-up (on by default): after each sync
    Purser posts a labelled "Balance adjustment" against Securities trade so
    the account's cash plus holdings equal the balance the broker reported
    in the same fetch (it covers a buy row the broker never sent). false
    stops future adjustments; past ones stay.
    name is the display name only: the account's path, which tools, rules
    and outside integrations use to find it, never changes, so a rename breaks
    nothing that refers to the account. The name is also
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
    app too.
    statement_close_day, on a credit card, is the day of the month its
    statement closes (1..31; a day past a month's end means its last day; 0
    clears it). With it, payments to the card whose schedule sets
    payment_amount are sized from the statement cycle and resized at once:
    what remains of the last statement, then a projection, then the average
    of past statements. card_statement_cycle suggests the day from the
    card's history; set it only when the user confirms or a statement says
    so."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service

        principal = _principal()
        sent = (
            name,
            description,
            hidden,
            min_balance,
            asset_kind,
            secured_by,
            revalue_after_days,
            balance_true_up,
            statement_close_day,
        )
        if all(v is None for v in sent):
            raise ValueError(
                "nothing to change: pass name, description, hidden, min_balance, asset_kind, "
                "secured_by, revalue_after_days, balance_true_up or statement_close_day"
            )
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
            balance_true_up=balance_true_up,
            statement_close_day=statement_close_day or None,
            statement_close_day_set=statement_close_day is not None,
            actor_token_id=principal.token_id,
        )
        from purser import assets as assets_service

        out: dict[str, Any] = {
            "id": str(updated.id),
            "path": updated.path,
            "name": updated.name,
            "hidden": updated.hidden_at is not None,
            "min_balance": _amount(updated.min_balance),
        }
        if updated.type == "brokerage":
            out["balance_true_up"] = updated.balance_true_up
        if updated.type == "credit_card":
            out["statement_close_day"] = updated.statement_close_day
        if asset_kind is not None or revalue_after_days is not None:
            row = assets_service.update_details(
                session,
                account=updated,
                kind=asset_kind.strip().lower() if asset_kind is not None else None,
                revalue_after_days=revalue_after_days or None,
                revalue_after_days_set=revalue_after_days is not None,
                actor_token_id=principal.token_id,
            )
            out["asset_kind"] = row.kind
            out["revalue_after_days"] = row.revalue_after_days
        if secured_by is not None:
            asset = (
                ledger_service.resolve_account(session, secured_by.strip())
                if secured_by.strip()
                else None
            )
            assets_service.secure_loan(
                session,
                loan=updated,
                asset_id=asset.id if asset else None,
                actor_token_id=principal.token_id,
            )
            out["secured_by"] = asset.path if asset else None
        return out

    return await _run(op)


@audited_tool(kind="write", entity="account", scope="write", emits=NOTHING)
async def reconcile_account(
    account: str,
    as_of: str,
    statement_balance: str,
    basis: str = "posted",
    dry_run: bool = False,
    component: Literal["balance", "cash"] = "balance",
    note: str | None = None,
) -> dict[str, Any]:
    """Reconcile a checking, savings, cash or credit-card account to a
    statement, or set a hand-valued asset's value on a date (write scope).
    statement_balance is the figure AS THE STATEMENT SHOWS IT: positive =
    money in the account, or for a credit card the amount owed. Purser
    converts it to ledger sign (cards are negative when owed), and
    every other amount in the answer is ledger sign. basis is posted (default)
    or pending_inclusive (posted plus pending); as_of is YYYY-MM-DD, today or
    earlier. Purser posts ONE adjustment for the difference to a system
    category; it needs review until it is recategorized or split to what it
    was, or accepted (accept_reconciliation_adjustment). When the ledger
    already matches nothing is posted and the clean reconcile is still
    recorded. dry_run=true
    answers the same without writing. Retrying the same date and balance
    returns created=false while it still holds; once a backdated change has
    moved the balance on that date it fails with the difference.
    For a manual brokerage account, component="cash" is REQUIRED and
    statement_balance must be cash only, excluding securities and unvested
    awards. Holdings and prices never change. Other accounts use the default
    component="balance". Results and list_accounts identify which component
    was reconciled; list_accounts checks whether it still holds.
    A hand-valued asset (type property: a house, a car) reconciles to its
    value: statement_balance is what it is worth on as_of, and the difference
    posts against the system account equity.revaluation as a change in value
    (category in the answer), so net worth moves and spending does not. One
    value per date; a date before the latest one is refused (record a
    backdated change with record_value_change instead). note says where the
    figure came from (an appraisal, an estimate) and is kept on the entry."""

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
            component=component,
            basis=basis,
            dry_run=dry_run,
            token=_token_row(session, principal),
            note=note,
        )
        return {
            "reconciliation_id": (
                str(outcome.reconciliation_id) if outcome.reconciliation_id else None
            ),
            "account_id": str(outcome.account_id),
            "as_of": outcome.as_of.isoformat(),
            "basis": outcome.basis,
            "component": outcome.component,
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


@audited_tool(kind="write", entity="account", scope="write", emits=NOTHING)
async def set_starting_balance(
    account: str,
    remove: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Start a bank-fed checking, savings, cash or credit-card account from
    the balance its bank reported in the latest sync (write scope). Use it
    when account_balance shows bank_snapshot.comparison.difference far from
    zero on an account that has no starting balance yet: a bank feed brings
    about three months of transactions and nothing from before them.
    You pass only the account. Purser takes the bank's figure, date, sign and
    basis, and posts ONE entry for the difference against the system account
    equity.opening_balances (neither income nor spending), dated the day
    before the account's earliest transaction.
    Afterwards the comparison's difference is zero. Purser keeps the entry in
    step by itself: when older history is imported it gives way by exactly
    those rows, so do not correct it. Mapping a new account in the UI does
    this by default, so most accounts already have one.
    Safe to repeat: status is set (this call), unchanged (it already had
    one), removed or none. Amounts are ledger sign (a card is negative when
    owed). It is refused, with the reason, when the bank's balance cannot be
    used (no balance, stale, another currency, not this account type) or when
    the account is reconciled to a statement and differs from the bank:
    reconcile a later statement with reconcile_account then.
    remove=true takes the starting balance out again. dry_run=true answers
    the same without writing."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser import starting_balance as service

        principal = _principal()
        account_id = ledger_service.resolve_account(session, account).id
        call = service.remove if remove else service.set_from_bank
        outcome = call(
            session, account_id=account_id, actor_token_id=principal.token_id, dry_run=dry_run
        )
        return {
            "account_id": str(outcome.account.id),
            "account": outcome.account.path,
            "status": outcome.status,
            "amount": _amount(outcome.amount),
            "entry_date": outcome.entry_date.isoformat() if outcome.entry_date else None,
            "basis": outcome.basis,
            "bank_balance": _amount(outcome.reported) if outcome.reported is not None else None,
            "bank_balance_date": (
                outcome.balance_date.isoformat() if outcome.balance_date else None
            ),
            "transaction_id": str(outcome.transaction_id) if outcome.transaction_id else None,
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
    """Record a loan account's value on a date (write scope). A loan's
    balance IS its latest valuation, in net worth and on every account
    surface, so this is how it is kept current: it cannot be reconciled
    (reconcile_account is for checking, savings, cash, credit cards and
    hand-valued assets). A hand-valued asset (type property) is refused here:
    its value is a ledger, changed with record_value_change or set on a date
    with reconcile_account. as_of is YYYY-MM-DD. value is a decimal string in
    ledger sign by default (convention='ledger'): a loan NEGATIVE while money
    is owed. To enter the figure as the statement prints it, a loan's
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
async def map_feed_accounts(
    accounts: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Map the bank accounts SimpleFIN delivers onto ledger accounts (write
    scope). A delivered account is not in Purser, and nothing from it is
    imported, until it is mapped; feed_status lists the ones waiting as
    not_connected.
    Call it with no arguments first: it answers the plan and writes nothing.
    Each line is one waiting account with Purser's proposal: name, type, path
    and whether to start from the bank's balance. SimpleFIN states no account
    type, so type_basis says what the type rests on: name (the bank's own
    words), holdings (the name says nothing and the feed lists holdings),
    balance (a debt, with or without credit left) or default (nothing says;
    offered as checking). A line with confident=false needs the person's
    answer; its note says so when the bank names a cash product but also
    lists holdings in it (ask whether it is an investment account, which is
    type brokerage). A line with action=ignore is
    the same account arriving through a second login at the same bank
    (duplicate_of names the first); action=blocked cannot be mapped yet.
    Show the person the plan and get their yes before applying it: AN
    ACCOUNT'S TYPE CANNOT BE CHANGED AFTER MAPPING, and undoing a mapping
    needs the delete-account command on the host.
    Then call it with accounts=[{"ref": ...}, ...], one line per account to
    act on. A line with only ref takes the proposal. Override any of name,
    type (checking, savings, credit_card, cash, brokerage, loan, property),
    path, starting_balance; give account (a path or id) to map onto an
    existing open account instead of a new one (refused, naming its feed,
    when that account already takes a feed: replacing one, replace_feed=true,
    needs an admin token, so ask the person to do it in Purser);
    action="ignore" leaves the account out of Purser. The whole batch is
    checked first and lands whole or not at all; dry_run=true answers the
    same without writing. Mapping imports what was already fetched, and
    starting_balance (the default for a new checking, savings, cash or
    credit-card account) makes the account match the bank at once. The
    bank's account ids and balances are never shown."""

    def op(session: Session) -> dict[str, Any]:
        from purser.admin import security
        from purser.feed import proposals

        principal = _principal()
        if accounts is None:
            return {"plan": [p.as_dict() for p in proposals.plan(session)]}
        results = proposals.apply(
            session,
            items=accounts,
            actor_token_id=principal.token_id,
            dry_run=dry_run,
            may_replace=security.is_admin(list(principal.scopes)),
        )
        return {"results": results, "dry_run": dry_run}

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
                line: dict[str, Any] = {
                    "connection_id": str(cid),
                    "imported": stats["imported"],
                    "ok": True,
                }
                repair = stats.get("gap_repair")
                if repair:  # accounts back from an outage (#481)
                    line["gap_repair"] = {
                        k: repair.get(k)
                        for k in ("accounts", "imported", "deferred", "unreachable_before")
                    }
                results.append(line)
            except feed_service.FeedError as exc:
                results.append({"connection_id": str(cid), "ok": False, "error": str(exc)})
        return {"synced": results}

    return await _run(op)


@audited_tool(kind="write", entity="connection", scope="write", emits=NOTHING)
async def refetch_feed(
    start: str,
    end: str | None = None,
    accounts: list[str] | None = None,
    connection_id: str | None = None,
) -> dict[str, Any]:
    """Re-fetch a date range from SimpleFIN and import what the ledger is
    missing — for a gap a bank outage left (a bank that stopped updating,
    then came back) that the automatic repair after a sync did not cover.
    start/end are ISO dates (end inclusive, default today; at most 90 days
    apart, and the bank serves about 90 days of history). accounts: ledger
    account paths (default every mapped account). connection_id may be
    omitted when there is one connection. Idempotent: rows already in the
    ledger are skipped. Uses one of the day's 24 fetches."""

    def op(session: Session) -> dict[str, Any]:
        from purser.feed import service as feed_service
        from purser.models import Connection

        principal = _principal()
        if connection_id is not None:
            cid = uuid.UUID(connection_id)
        else:
            ids = list(
                session.execute(
                    select(Connection.id).where(Connection.status != "disabled")
                ).scalars()
            )
            if len(ids) != 1:
                raise ValueError("several connections: pass connection_id")
            cid = ids[0]
        result = feed_service.refetch_connection(
            session,
            connection_id=cid,
            start=date_type.fromisoformat(start),
            end=date_type.fromisoformat(end) if end else None,
            accounts=accounts,
            actor_token_id=principal.token_id,
        )
        return {
            "start": result["start"].isoformat(),
            "end": result["end"].isoformat(),
            "accounts": result["accounts"],
            "imported": result["imported"],
            "skipped": result["skipped"],
        }

    return await _run(op)
