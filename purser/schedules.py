"""Schedules service: bills as first-class objects (DESIGN §6).

Bill sources: manual, detected-from-history (confirmation required, never
auto-activated), agent-written via MCP (``propose=True`` -> a suggestion, #436).

The schedule is the durable *rule*; its *instances* are ``expected``
transactions materialized on a rolling horizon and consumed by the matcher
and the forecaster (decision 55). Instances are the thing users edit — skip
this month, override the amount, "it landed two days early" — so the rule
never mutates them behind the user's back: a token-bearing override (amount,
date, category) pins the instance (``schedule_pinned_at``), and pinned
instances are exempt from estimate refresh and from rule-change/pause
resyncs. Labels are different (decision 79): a description or payee edit
does not pin, and renaming the rule relabels every live instance — pinned
ones too — whose label still equals the old name. ``reset_instance`` hands a
pinned instance back to the rule.

Slot bookkeeping: an instance fills the rule slot ``occurrence_date``.
Materialization is idempotent on (schedule_id, occurrence_date) regardless
of status, so a user skip (void) keeps the slot occupied — the bill does not
come back — while a resync (rule edit, pause, end) voids the instance AND
releases its slot (``occurrence_date = NULL``) so a later rule can fill it
again.
"""

import itertools
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from purser import audit, crypto, events
from purser import ledger as ledger_service
from purser import networth as networth_service
from purser.admin import security
from purser.core import forecasting as core_forecasting
from purser.core import ledger as core
from purser.core import matching as core_matching
from purser.core import recurrence as core_recurrence
from purser.ledger import LedgerError, NotFoundError
from purser.models import (
    Account,
    ApiToken,
    InstanceStatement,
    Match,
    Payee,
    Posting,
    Schedule,
    Suggestion,
    Transaction,
)

MATERIALIZE_HORIZON_DAYS = 60
DETECTION_LOOKBACK_DAYS = 400
NEXT_DUE_LOOKAHEAD_DAYS = 366

AMOUNT_TYPES = ("fixed", "estimated")
ESTIMATE_BASES = tuple(b.value for b in core_forecasting.EstimateBasis)
AUTOPOST_POLICIES = ("auto_post", "wait_for_match", "reminder_only")
# How far a card statement's due date may sit from the payment instance it
# addresses by card (#351): a statement names the cycle's due date, and the
# instance's date is the rule's slot or the user's override.
STATEMENT_DUE_WINDOW_DAYS = 14


def get_schedule(session: Session, schedule_id: uuid.UUID) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise NotFoundError(f"schedule {schedule_id} not found")
    return schedule


def _validate_rule(rrule: str, dtstart: date_type) -> list[date_type]:
    """FREQ allowlist + a real expansion (dateutil is the parser); returns the
    upcoming occurrences from today so callers can seed ``next_due``."""
    today = date_type.today()
    try:
        core_forecasting.validate_rrule(rrule)
        return core_forecasting.expand_rrule(
            rrule,
            dtstart=dtstart,
            start=today,
            end=today + timedelta(days=NEXT_DUE_LOOKAHEAD_DAYS),
        )
    except (ValueError, KeyError) as exc:
        raise LedgerError(f"invalid RRULE: {exc}") from exc


def _validate_amount_fields(
    *,
    amount_type: str,
    amount: Decimal | None,
    estimate_basis: str | None,
    has_counterparty: bool = False,
) -> str | None:
    """Cross-field rules shared by create and update; returns the effective
    estimate basis."""
    if amount_type not in AMOUNT_TYPES:
        raise LedgerError("amount_type must be fixed | estimated")
    if amount_type == "fixed":
        if amount is None:
            raise LedgerError("fixed schedules need an amount")
        return None
    basis = estimate_basis or core_forecasting.EstimateBasis.ROLLING_AVG_3.value
    if basis not in ESTIMATE_BASES:
        raise LedgerError(f"estimate_basis must be one of {', '.join(ESTIMATE_BASES)}")
    if basis == core_forecasting.EstimateBasis.COUNTERPARTY_BALANCE.value and not has_counterparty:
        raise LedgerError("estimate_basis=counterparty_balance requires a counterparty account")
    return basis


def _resolve_counterparty(
    session: Session, account: Account, counterparty_ref: str | None
) -> Account | None:
    """The other real account of a transfer schedule (#232): real, open,
    distinct, same currency."""
    if counterparty_ref is None:
        return None
    counterparty = ledger_service.resolve_account(session, counterparty_ref)
    if counterparty.is_virtual:
        raise LedgerError(f"{counterparty.path} is a category; a counterparty is a real account")
    if counterparty.id == account.id:
        raise LedgerError("a transfer schedule needs two different accounts")
    if counterparty.closed_at is not None:
        raise LedgerError(f"{counterparty.path} is closed")
    if counterparty.currency.strip() != account.currency.strip():
        raise LedgerError("currency mismatch (v1 has no FX)")
    return counterparty


def create_schedule(
    session: Session,
    *,
    name: str,
    account_ref: str,
    rrule: str,
    amount_type: str,
    amount: Decimal | None,
    token: ApiToken | None,
    payee_name: str | None = None,
    category_path: str | None = None,
    estimate_basis: str | None = None,
    autopost_policy: str = "wait_for_match",
    source: str = "manual",
    dtstart: date_type | None = None,
    counterparty_account_ref: str | None = None,
    note: str | None = None,
    propose: bool = False,
) -> Schedule | Suggestion:
    """Create an ACTIVE schedule and materialize it. With ``propose`` the
    caller asks instead (#436): a **suggestion** carrying the validated draft
    (#233, decision 57) lands in the inbox for the user to decide. Proposing
    is the caller's choice per call, not a ceiling set by its token: an agent
    carrying out the user's request writes directly, one acting on its own
    initiative proposes.

    A proposal is refused when the account already has a schedule for that
    payee or counterparty, and carries no confidence: nothing scored it.
    ``note`` is its evidence for the person deciding it (#250); a direct
    create has no suggestion, so it keeps no note."""
    from purser import suggestions as suggestions_service

    note = suggestions_service.agent_note(note)
    account = ledger_service.resolve_account(session, account_ref)
    if account.is_virtual:
        raise LedgerError("schedules attach to real accounts")
    if account.closed_at is not None:
        raise LedgerError(f"{account.path} is closed; reopen it before scheduling against it")
    if autopost_policy not in AUTOPOST_POLICIES:
        raise LedgerError("autopost_policy must be auto_post | wait_for_match | reminder_only")
    if counterparty_account_ref is not None and category_path is not None:
        raise LedgerError("a schedule is either a transfer (counterparty) or flat (category)")
    counterparty = _resolve_counterparty(session, account, counterparty_account_ref)
    estimate_basis = _validate_amount_fields(
        amount_type=amount_type,
        amount=amount,
        estimate_basis=estimate_basis,
        has_counterparty=counterparty is not None,
    )
    if category_path is not None:
        category = ledger_service.resolve_account(session, category_path)
        if not category.is_virtual:
            raise LedgerError(f"{category.path} is not a category (virtual account)")
        category_path = category.path
    anchor = dtstart or date_type.today()
    upcoming = _validate_rule(rrule, anchor)

    if propose:
        if token is None:
            raise LedgerError("a proposal needs a token to attribute it to")
        payee = ledger_service.resolve_payee(session, payee_name) if payee_name else None
        existing = suggestions_service.existing_schedule_for(
            session,
            account_id=account.id,
            counterparty_account_id=counterparty.id if counterparty else None,
            payee_id=payee.id if payee else None,
        )
        if existing is not None:
            who = counterparty.path if counterparty else payee_name
            raise LedgerError(
                f"{account.path} already has a schedule for {who}: '{existing.name}' "
                f"({existing.status}, id {existing.id}), so there is nothing to propose. "
                "Changes to it go through update_schedule or end_schedule."
            )
        draft, _, _ = suggestions_service.draft_from_create_args(
            session,
            name=name,
            account_ref=account_ref,
            rrule=rrule,
            dtstart=anchor,
            amount_type=amount_type,
            amount=amount,
            estimate_basis=estimate_basis,
            payee_name=payee_name,
            category_path=category_path,
            autopost_policy=autopost_policy,
            counterparty_account_ref=counterparty_account_ref,
        )
        key = suggestions_service.counterparty_key_for(
            counterparty_account_id=counterparty.id if counterparty else None,
            payee_bidx=payee.name_bidx if payee else None,
            name=name,
        )
        row, _created = suggestions_service.upsert_schedule_suggestion(
            session,
            draft=draft,
            source="agent",
            fingerprint=suggestions_service.schedule_fingerprint(account.id, key),
            subject_account_id=account.id,
            subject_payee_id=payee.id if payee else None,
            counterparty_account_id=counterparty.id if counterparty else None,
            evidence={"note": note} if note else None,
            # NULL, not 1.000: no evidence was scored, so the card claims no
            # certainty and the inbox sorts the row after scored ones (#250).
            confidence=None,
            detector="agent",
            detector_version=1,
            actor_token_id=token.id,
        )
        return row

    status = "active"
    schedule = Schedule(
        name=name,
        account_id=account.id,
        payee_id=ledger_service.resolve_payee(session, payee_name).id if payee_name else None,
        category_path=category_path,
        counterparty_account_id=counterparty.id if counterparty is not None else None,
        rrule=rrule,
        dtstart=anchor,
        amount_type=amount_type,
        amount=amount,
        estimate_basis=estimate_basis,
        currency=account.currency.strip(),
        next_due=upcoming[0] if upcoming else None,
        autopost_policy=autopost_policy,
        source=source,
        status=status,
    )
    session.add(schedule)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="schedule.create",
        entity_type="schedule",
        entity_id=schedule.id,
        after={
            "name": name,
            "rrule": rrule,
            "dtstart": anchor.isoformat(),
            "counterparty_account_id": str(counterparty.id) if counterparty else None,
            "source": source,
            "status": status,
        },
    )
    if status == "active":
        materialize_schedule(session, schedule)
    return schedule


def _require_write(token: ApiToken, what: str) -> None:
    if not security.can_write(token.scopes):
        raise LedgerError(f"{what} requires write scope")


def _schedule_audit_shape(schedule: Schedule) -> dict[str, Any]:
    return {
        "name": schedule.name,
        "rrule": schedule.rrule,
        "dtstart": schedule.dtstart.isoformat(),
        "amount_type": schedule.amount_type,
        "amount": str(schedule.amount) if schedule.amount is not None else None,
        "estimate_basis": schedule.estimate_basis,
        "category_path": str(schedule.category_path) if schedule.category_path else None,
        "counterparty_account_id": (
            str(schedule.counterparty_account_id) if schedule.counterparty_account_id else None
        ),
        "payee_id": str(schedule.payee_id) if schedule.payee_id else None,
        "autopost_policy": schedule.autopost_policy,
        "status": schedule.status,
    }


@dataclass(frozen=True)
class UpdatedSchedule:
    """``update_schedule``'s result: the rule, and how many live instances
    the edit relabeled."""

    schedule: Schedule
    relabeled: int


def _rule_payee_name(session: Session, schedule: Schedule) -> str | None:
    """The payee the rule writes on its instances: its own payee, else (a
    flat schedule without one) its name; a transfer's instances have none."""
    if schedule.counterparty_account_id is not None:
        return None
    payee = session.get(Payee, schedule.payee_id) if schedule.payee_id else None
    return payee.name if payee is not None else schedule.name


def _relabel_instances(
    session: Session,
    schedule: Schedule,
    *,
    old_name: str,
    old_payee_name: str | None,
    actor_token_id: uuid.UUID | None,
) -> int:
    """Labels follow the rule (decision 79): every live instance, pinned or
    not, whose description is still the old name takes the new one, and one
    whose payee is still the old rule payee (normalized) takes the new rule
    payee. A label the user changed differs from the old one and is kept.
    Never touches the pin, amounts, dates or slots. One ``schedule.relabel``
    audit row lists the instances; a no-op writes nothing."""
    new_payee_name = _rule_payee_name(session, schedule)
    rename = schedule.name != old_name
    repayee = (
        old_payee_name is not None
        and new_payee_name is not None
        and core.normalize_payee(old_payee_name) != core.normalize_payee(new_payee_name)
    )
    if not rename and not repayee:
        return 0
    old_payee_bidx = (
        crypto.blind_index(core.normalize_payee(old_payee_name)) if old_payee_name else None
    )
    changed: list[str] = []
    for tx in _live_instances(session, schedule):
        touched = False
        if rename and tx.description == old_name:
            tx.description = schedule.name
            touched = True
        if (
            repayee
            and new_payee_name is not None
            and tx.payee is not None
            and tx.payee.name_bidx == old_payee_bidx
        ):
            tx.payee = ledger_service.resolve_payee(session, new_payee_name)
            touched = True
        if touched:
            changed.append(str(tx.id))
    if not changed:
        return 0
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="schedule.relabel",
        entity_type="schedule",
        entity_id=schedule.id,
        before={"name": old_name, "payee": old_payee_name},
        after={"name": schedule.name, "payee": new_payee_name, "instances": changed},
    )
    return len(changed)


def update_schedule(
    session: Session,
    *,
    schedule_id: uuid.UUID,
    token: ApiToken,
    name: str | None = None,
    rrule: str | None = None,
    dtstart: date_type | None = None,
    amount_type: str | None = None,
    amount: Decimal | None = None,
    estimate_basis: str | None = None,
    category_path: str | None = None,
    payee_name: str | None = None,
    autopost_policy: str | None = None,
    counterparty_account_ref: str | None = None,
) -> UpdatedSchedule:
    """Edit the rule (write scope). Live, un-pinned instances are resynced:
    slots the new rule no longer produces are voided-and-released, amounts
    follow the new basis, missing slots materialize. Pinned instances keep
    their amounts, dates and slots. Changing the shape (category <->
    counterparty) releases every un-pinned instance — their legs are wrong.
    A new name or payee relabels live instances, pinned ones included,
    whose label still equals the old one (decision 79)."""
    _require_write(token, "editing a schedule")
    schedule = get_schedule(session, schedule_id)
    if schedule.status == "ended":
        raise LedgerError("an ended schedule is terminal; create a new one")
    before = _schedule_audit_shape(schedule)
    old_name = schedule.name
    old_payee_name = _rule_payee_name(session, schedule)
    account = session.get(Account, schedule.account_id)
    assert account is not None

    shape_changed = False
    if counterparty_account_ref is not None and category_path is not None:
        raise LedgerError("a schedule is either a transfer (counterparty) or flat (category)")
    if counterparty_account_ref is not None:
        counterparty = _resolve_counterparty(session, account, counterparty_account_ref)
        assert counterparty is not None
        shape_changed = schedule.counterparty_account_id != counterparty.id
        schedule.counterparty_account_id = counterparty.id
        schedule.category_path = None
    if category_path is not None:
        category = ledger_service.resolve_account(session, category_path)
        if not category.is_virtual:
            raise LedgerError(f"{category.path} is not a category (virtual account)")
        shape_changed = shape_changed or schedule.counterparty_account_id is not None
        shape_changed = shape_changed or str(schedule.category_path) != category.path
        schedule.category_path = category.path
        schedule.counterparty_account_id = None

    new_type = amount_type or schedule.amount_type
    new_amount = amount if amount is not None else schedule.amount
    new_basis = estimate_basis or (
        schedule.estimate_basis if new_type == schedule.amount_type else None
    )
    schedule.estimate_basis = _validate_amount_fields(
        amount_type=new_type,
        amount=new_amount,
        estimate_basis=new_basis,
        has_counterparty=schedule.counterparty_account_id is not None,
    )
    schedule.amount_type = new_type
    schedule.amount = new_amount
    if autopost_policy is not None:
        if autopost_policy not in AUTOPOST_POLICIES:
            raise LedgerError("autopost_policy must be auto_post | wait_for_match | reminder_only")
        schedule.autopost_policy = autopost_policy
    if payee_name is not None:
        schedule.payee_id = ledger_service.resolve_payee(session, payee_name).id
    if name is not None:
        schedule.name = name
    if rrule is not None or dtstart is not None:
        new_rrule = rrule or schedule.rrule
        new_anchor = dtstart or schedule.dtstart
        _validate_rule(new_rrule, new_anchor)
        schedule.rrule = new_rrule
        schedule.dtstart = new_anchor
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id,
        action="schedule.update",
        entity_type="schedule",
        entity_id=schedule.id,
        before=before,
        after=_schedule_audit_shape(schedule),
    )
    if schedule.status == "active":
        if shape_changed:
            _release_live_instances(session, schedule, actor_token_id=token.id, reason="reshape")
        else:
            _resync_instances(session, schedule, actor_token_id=token.id)
        materialize_schedule(session, schedule)
    else:
        refresh_next_due(session, schedule)
    # After the resync: what it released is gone, what it materialized
    # already carries the new labels, and a paused rule's pinned survivors
    # are relabeled too.
    relabeled = _relabel_instances(
        session,
        schedule,
        old_name=old_name,
        old_payee_name=old_payee_name,
        actor_token_id=token.id,
    )
    return UpdatedSchedule(schedule=schedule, relabeled=relabeled)


def _set_status(
    session: Session, schedule: Schedule, *, status: str, token: ApiToken, action: str
) -> None:
    before = schedule.status
    schedule.status = status
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id,
        action=action,
        entity_type="schedule",
        entity_id=schedule.id,
        before={"status": before},
        after={"status": status},
    )


def pause_schedule(session: Session, *, schedule_id: uuid.UUID, token: ApiToken) -> Schedule:
    """Stop expecting: live un-pinned instances are voided and their slots
    released, so resume re-materializes them. Pinned instances survive."""
    _require_write(token, "pausing a schedule")
    schedule = get_schedule(session, schedule_id)
    if schedule.status != "active":
        raise LedgerError(f"only active schedules pause (status is {schedule.status})")
    _set_status(session, schedule, status="paused", token=token, action="schedule.paused")
    _release_live_instances(session, schedule, actor_token_id=token.id, reason="pause")
    refresh_next_due(session, schedule)
    return schedule


def resume_schedule(session: Session, *, schedule_id: uuid.UUID, token: ApiToken) -> Schedule:
    """paused -> active; materializes immediately."""
    _require_write(token, "activating a schedule")
    schedule = get_schedule(session, schedule_id)
    if schedule.status == "ended":
        raise LedgerError("an ended schedule is terminal; create a new one")
    if schedule.status == "active":
        return schedule
    account = session.get(Account, schedule.account_id)
    if account is not None and account.closed_at is not None:
        raise LedgerError(f"{account.path} is closed; reopen it before activating schedules")
    _set_status(session, schedule, status="active", token=token, action="schedule.active")
    materialize_schedule(session, schedule)
    return schedule


def end_schedule(session: Session, *, schedule_id: uuid.UUID, token: ApiToken) -> Schedule:
    """Terminal. Live un-pinned instances are voided (slots released, though
    nothing will fill them again); history and matched instances are kept."""
    _require_write(token, "ending a schedule")
    schedule = get_schedule(session, schedule_id)
    if schedule.status == "ended":
        return schedule
    _set_status(session, schedule, status="ended", token=token, action="schedule.ended")
    schedule.ended_at = datetime.now(UTC)
    _release_live_instances(session, schedule, actor_token_id=token.id, reason="end")
    schedule.next_due = None
    session.flush()
    return schedule


RECURRING_ITEM_CAP = 8  # keeps the structuredContent inside guardrail 6's budget


def list_recurring(session: Session) -> dict[str, Any]:
    """The subscription radar (issue #74): every outflow schedule with its
    monthly-normalized cost, plus price-change history derived from
    confirmed-match actuals. Totals are computed server-side over ALL rows
    (guardrail 6); items are the largest monthly costs first, capped with a
    truncated flag. Detected-but-unconfirmed patterns live in the
    suggestions inbox (#233); ``suggestions_open`` says how many wait."""
    today = date_type.today()
    year_ago = today - timedelta(days=365)
    schedules = list(
        session.execute(
            select(Schedule).where(Schedule.status == "active").order_by(Schedule.next_due)
        ).scalars()
    )

    payee_ids = {s.payee_id for s in schedules if s.payee_id is not None}
    payee_names: dict[uuid.UUID, str] = (
        {
            pid: name
            for pid, name in session.execute(
                select(Payee.id, Payee.name).where(Payee.id.in_(payee_ids))
            )
        }
        if payee_ids
        else {}
    )

    items: list[dict[str, Any]] = []
    monthly_total = Decimal(0)
    changes_count = 0
    changes_delta = Decimal(0)
    for schedule in schedules:
        amount = instance_amount(session, schedule)
        if amount >= 0:
            continue  # income schedules stay in list_schedules; this is "what am I paying"
        cost = -amount
        try:
            monthly_equiv = core_forecasting.monthly_equivalent(schedule.rrule, cost)
        except ValueError:
            monthly_equiv = cost.quantize(Decimal("0.01"))  # unknown cadence: assume monthly

        actuals = matched_actuals(session, schedule)
        last_change: dict[str, str] | None = None
        for (newer_amount, newer_date), (older_amount, _) in itertools.pairwise(actuals):
            step = abs(newer_amount) - abs(older_amount)
            if step != 0:
                if last_change is None:
                    last_change = {
                        "date": newer_date.isoformat(),
                        "delta": str(step.quantize(Decimal("0.01"))),
                    }
                if newer_date >= year_ago:
                    changes_count += 1
                    changes_delta += step
        payee = payee_names.get(schedule.payee_id) if schedule.payee_id is not None else None
        items.append(
            {
                "schedule_id": str(schedule.id),
                "payee": payee or schedule.name,
                "cadence": core_forecasting.cadence_label(schedule.rrule),
                "amount": str(cost.quantize(Decimal("0.01"))),
                "monthly_equiv": str(monthly_equiv),
                "next_due": schedule.next_due.isoformat() if schedule.next_due else None,
                "source": schedule.source,
                "last_change": last_change,
            }
        )
        monthly_total += monthly_equiv

    items.sort(key=lambda i: Decimal(i["monthly_equiv"]), reverse=True)
    truncated = len(items) > RECURRING_ITEM_CAP
    from purser import suggestions as suggestions_service

    return {
        "monthly_total": str(monthly_total.quantize(Decimal("0.01"))),
        "annualized_total": str((monthly_total * 12).quantize(Decimal("0.01"))),
        "count": len(items),
        "changes_12mo": {
            "count": changes_count,
            "delta": str(changes_delta.quantize(Decimal("0.01"))),
        },
        "items": items[:RECURRING_ITEM_CAP],
        "truncated": truncated,
        "suggestions_open": suggestions_service.open_count(
            session, kind=suggestions_service.KIND_SCHEDULE
        ),
    }


# ---------------------------------------------------------------------------
# Materialization: schedule instances -> expected transactions
# ---------------------------------------------------------------------------


CHAIN_MAX_HOPS = 32  # lineage walks are 1-3 hops in practice; this only bounds a cycle


def chain_heads(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Each row's lineage head, walked set-wise (a few queries, not one per
    row): a void row follows ``superseded_by`` to what replaced it — its
    posted successor, the transfer it merged into — and a corrected row
    follows its correction chain (original <- reversal <- replacement, via
    ``reverses_id``), until neither link applies."""
    heads = {row_id: row_id for row_id in ids}
    for _ in range(CHAIN_MAX_HOPS):
        frontier = set(heads.values())
        step: dict[uuid.UUID, uuid.UUID] = {
            row.id: row.superseded_by
            for row in session.execute(
                select(Transaction.id, Transaction.superseded_by).where(
                    Transaction.id.in_(frontier), Transaction.superseded_by.is_not(None)
                )
            )
        }
        unsuperseded = frontier - step.keys()
        if unsuperseded:
            for row in session.execute(
                select(Transaction.reverses_id, Transaction.id).where(
                    Transaction.reverses_id.in_(unsuperseded)
                )
            ):
                step[row.reverses_id] = row.id
        if not step:
            break
        heads = {start: step.get(head, head) for start, head in heads.items()}
    return heads


def matched_actuals(session: Session, schedule: Schedule) -> list[tuple[Decimal, date_type]]:
    """(amount, date) of what settled each of the schedule's matched
    instances, newest first — the estimate basis and the price history (#74).

    Posted amount wins (DESIGN §5, decision 69): each confirmed match is
    followed from the bank row it names to that row's live chain head (a
    pending to its posted successor, a merged row to its transfer, a
    corrected row to its replacement), and only a POSTED head counts — a
    pending amount is provisional, and a void head (the bank dropped the row)
    settled nothing. A head that two matches reach counts once.

    ONE amount per settling row, seen from the schedule's account (#270): its
    leg there, or for a transfer settled from the far side the negated
    counterparty leg. A merged transfer has two real legs, and reading both
    fed the estimate an opposite pair per payment."""
    instance_ids = select(Transaction.id).where(Transaction.schedule_id == schedule.id)
    matched = list(
        session.execute(
            select(Match.posted_id).where(
                Match.state == "confirmed", Match.expected_id.in_(instance_ids)
            )
        ).scalars()
    )
    if not matched:
        return []
    heads = set(chain_heads(session, matched).values())
    rows = session.execute(
        select(Posting.transaction_id, Posting.account_id, Posting.amount, Transaction.date)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Transaction.id.in_(heads),
            Transaction.status == core.Status.POSTED.value,
            Account.is_virtual.is_(False),
        )
        .order_by(Transaction.date.desc(), Transaction.created_at.desc(), Transaction.id)
    ).all()
    legs: dict[uuid.UUID, list[Any]] = {}
    for row in rows:
        legs.setdefault(row.transaction_id, []).append(row)
    out: list[tuple[Decimal, date_type]] = []
    for group in legs.values():
        own = next((r for r in group if r.account_id == schedule.account_id), None)
        far = next(
            (r for r in group if r.account_id == schedule.counterparty_account_id),
            None,
        )
        if own is not None:
            out.append((Decimal(own.amount), own.date))
        elif far is not None:
            out.append((-Decimal(far.amount), far.date))
        elif len(group) == 1:
            out.append((Decimal(group[0].amount), group[0].date))
    return out


def _matched_actuals(session: Session, schedule: Schedule) -> list[Decimal]:
    return [amount for amount, _ in matched_actuals(session, schedule)]


@dataclass(frozen=True)
class ScheduleEstimate:
    """What a schedule's rule says an occurrence is (#247): the ``amount``
    the next instance materializes with (the schedule account's leg), its
    ``basis`` (``fixed`` or the estimate basis), and the settled ``actuals``
    behind it, newest first — empty for a fixed schedule, which needs none.
    The estimate range band reads ``actuals``."""

    amount: Decimal
    basis: str
    actuals: tuple[Decimal, ...] = ()


def schedule_estimate(session: Session, schedule: Schedule) -> ScheduleEstimate:
    """The schedule's estimate: fixed, or estimated from confirmed-match
    history per its basis — for ``counterparty_balance`` the counterparty's
    reported balance, with the past payments as its actuals (#247)."""
    fallback = Decimal(schedule.amount) if schedule.amount is not None else Decimal(0)
    if schedule.amount_type == "fixed":
        return ScheduleEstimate(amount=fallback, basis=core_forecasting.BASIS_FIXED)
    basis = core_forecasting.EstimateBasis(schedule.estimate_basis or "rolling_avg_3")
    actuals = tuple(_matched_actuals(session, schedule))
    if (
        basis is core_forecasting.EstimateBasis.COUNTERPARTY_BALANCE
        and schedule.counterparty_account_id is not None
    ):
        # "Pay the balance": the counterparty's reported balance is what the
        # schedule account will send. A card's balance is negative when money
        # is owed, so the checking leg IS that balance; nothing owed (or a
        # credit) means no instance until it goes negative again (#232).
        owed = networth_service.reported_balances(session, [schedule.counterparty_account_id])[
            schedule.counterparty_account_id
        ].balance
        amount = core.quantize(owed) if owed < 0 else Decimal(0)
        return ScheduleEstimate(amount=amount, basis=basis.value, actuals=actuals)
    return ScheduleEstimate(
        amount=core_forecasting.estimate_amount(basis, list(actuals), fallback),
        basis=basis.value,
        actuals=actuals,
    )


def instance_amount(session: Session, schedule: Schedule) -> Decimal:
    """The amount the next instance materializes with (``schedule_estimate``)."""
    return schedule_estimate(session, schedule).amount


_instance_amount = instance_amount  # internal call sites predate the public name


def _live_instances(session: Session, schedule: Schedule) -> list[Transaction]:
    """Unmatched expected instances of a schedule (any date — an overdue one
    is still due), postings loaded."""
    return list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings))
            .where(
                Transaction.schedule_id == schedule.id,
                Transaction.status == core.Status.EXPECTED.value,
                Transaction.superseded_by.is_(None),
            )
            .order_by(Transaction.date)
        ).scalars()
    )


def _void_and_release(
    session: Session, tx: Transaction, *, actor_token_id: uuid.UUID | None, reason: str
) -> None:
    """Resync void: the instance dies AND its slot is released, unlike a user
    skip (``transition_status`` -> void) which keeps ``occurrence_date`` so
    the bill never comes back on its own."""
    before = {
        "status": tx.status,
        "occurrence_date": (tx.occurrence_date.isoformat() if tx.occurrence_date else None),
    }
    tx.status = core.Status.VOID.value
    tx.occurrence_date = None
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="schedule.resync",
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after={"status": tx.status, "occurrence_date": None, "reason": reason},
    )


def _release_live_instances(
    session: Session, schedule: Schedule, *, actor_token_id: uuid.UUID | None, reason: str
) -> int:
    released = 0
    for tx in _live_instances(session, schedule):
        if tx.schedule_pinned_at is not None:
            continue  # the user's row, not the rule's
        _void_and_release(session, tx, actor_token_id=actor_token_id, reason=reason)
        released += 1
    return released


def _set_instance_amount(tx: Transaction, amount: Decimal, account_id: uuid.UUID) -> None:
    """Move an instance's amount: the schedule account's leg takes ``amount``
    and the other leg (category, or counterparty in #232) the negation, so the
    zero-sum invariant holds by construction."""
    for posting in tx.postings:
        posting.amount = amount if posting.account_id == account_id else -amount


def _rule_slots(schedule: Schedule) -> set[date_type]:
    """Every slot the rule produces around today — far enough back for an
    overdue instance, far enough ahead for the horizon."""
    today = date_type.today()
    return set(
        core_forecasting.expand_rrule(
            schedule.rrule,
            dtstart=schedule.dtstart,
            start=min(schedule.dtstart, today) - timedelta(days=NEXT_DUE_LOOKAHEAD_DAYS),
            end=today + timedelta(days=NEXT_DUE_LOOKAHEAD_DAYS),
        )
    )


def _resync_instances(
    session: Session, schedule: Schedule, *, actor_token_id: uuid.UUID | None
) -> None:
    """After a rule edit: void-and-release live un-pinned instances whose
    slot the rule no longer produces; re-estimate the survivors."""
    slots = _rule_slots(schedule)
    amount = _instance_amount(session, schedule)
    for tx in _live_instances(session, schedule):
        if tx.schedule_pinned_at is not None:
            continue
        if tx.occurrence_date not in slots:
            _void_and_release(session, tx, actor_token_id=actor_token_id, reason="rule_edit")
            continue
        if amount != 0:
            _set_instance_amount(tx, amount, schedule.account_id)
    session.flush()


def _refresh_estimates(
    session: Session, schedule: Schedule, amount: Decimal, *, actor_token_id: uuid.UUID | None
) -> int:
    """Estimated schedules follow their actuals: live, un-pinned instances
    whose amount differs from the current estimate move to it (#231). Fixed
    schedules never refresh — their amount only changes via update_schedule."""
    if schedule.amount_type != "estimated" or amount == 0:
        return 0
    moved: list[dict[str, str]] = []
    for tx in _live_instances(session, schedule):
        if tx.schedule_pinned_at is not None:
            continue
        leg = next((p for p in tx.postings if p.account_id == schedule.account_id), None)
        if leg is None or Decimal(leg.amount) == amount:
            continue
        moved.append({"transaction_id": str(tx.id), "from": str(leg.amount), "to": str(amount)})
        _set_instance_amount(tx, amount, schedule.account_id)
    if moved:
        session.flush()
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="schedule.refresh",
            entity_type="schedule",
            entity_id=schedule.id,
            after={"estimate": str(amount), "instances": moved},
        )
    return len(moved)


def refresh_next_due(session: Session, schedule: Schedule) -> date_type | None:
    """``next_due`` = the earliest live instance (overdue counts: it is still
    due), else the next rrule occurrence from today; NULL when paused/ended."""
    if schedule.status != "active":
        schedule.next_due = None
        session.flush()
        return None
    live = _live_instances(session, schedule)
    if live:
        schedule.next_due = live[0].date
    else:
        today = date_type.today()
        upcoming = core_forecasting.expand_rrule(
            schedule.rrule,
            dtstart=schedule.dtstart,
            start=today,
            end=today + timedelta(days=NEXT_DUE_LOOKAHEAD_DAYS),
        )
        schedule.next_due = upcoming[0] if upcoming else None
    session.flush()
    return schedule.next_due


def touch_instance(session: Session, tx: Transaction, *, pin: bool) -> None:
    """Hook for the ledger/matching write paths (#231): a token-bearing edit
    pins the instance; every instance change re-derives the schedule's
    ``next_due`` so the cache never drifts from the rows."""
    if tx.schedule_id is None:
        return
    if pin and tx.status == core.Status.EXPECTED.value:
        tx.schedule_pinned_at = datetime.now(UTC)
    schedule = session.get(Schedule, tx.schedule_id)
    if schedule is not None:
        refresh_next_due(session, schedule)


def _has_rule_shape(tx: Transaction, schedule: Schedule) -> bool:
    """Whether the instance's legs are the shape the rule writes now: a
    transfer between the schedule's two accounts, or one real leg on the
    schedule account plus one category leg. A pinned instance that survived
    a reshape (decision 56) is not."""
    real = {p.account_id for p in tx.postings if not p.account.is_virtual}
    virtual = [p for p in tx.postings if p.account.is_virtual]
    if schedule.counterparty_account_id is not None:
        return (
            len(tx.postings) == 2
            and not virtual
            and real == {schedule.account_id, schedule.counterparty_account_id}
        )
    return len(tx.postings) == 2 and len(virtual) == 1 and real == {schedule.account_id}


def _instance_audit_shape(tx: Transaction) -> dict[str, Any]:
    return {
        "status": tx.status,
        "date": tx.date.isoformat(),
        "occurrence_date": tx.occurrence_date.isoformat() if tx.occurrence_date else None,
        "description": tx.description,
        "payee": tx.payee.name if tx.payee else None,
        "pinned": tx.schedule_pinned_at is not None,
        "postings": [
            {"account_id": str(p.account_id), "amount": str(p.amount)} for p in tx.postings
        ],
    }


def reset_instance(session: Session, *, tx_id: uuid.UUID, token: ApiToken) -> Transaction:
    """Hand an instance back to the rule (write scope, decision 79): the pin
    is cleared and the date, label, payee, category and amount become what
    the rule writes for its slot today — both legs of a transfer move, so it
    stays zero-sum. When the rule no longer produces the slot, or estimates
    nothing, the instance is voided and its slot released, exactly as a
    resync would; an instance whose legs no longer have the rule's shape is
    released and the slot materialized afresh. Audited ``schedule.instance_reset``."""
    _require_write(token, "resetting a schedule instance")
    tx = ledger_service.get_transaction(session, tx_id)
    if (
        tx.schedule_id is None
        or tx.status != core.Status.EXPECTED.value
        or tx.superseded_by is not None
    ):
        raise LedgerError("not a live schedule instance")
    schedule = get_schedule(session, tx.schedule_id)
    if schedule.status != "active":
        raise LedgerError(
            f"only an active schedule's instances reset (status is {schedule.status})"
        )
    closed = sorted(str(p.account.path) for p in tx.postings if p.account.closed_at is not None)
    if closed:
        raise LedgerError(f"{closed[0]} is closed; reopen it before resetting its instances")
    before = _instance_audit_shape(tx)
    amount = _instance_amount(session, schedule)
    in_rule = tx.occurrence_date is not None and tx.occurrence_date in _rule_slots(schedule)
    reshaped = not _has_rule_shape(tx, schedule)
    tx.schedule_pinned_at = None
    # A recorded statement is an override too (decision 80): handing the
    # instance back to the rule drops it, or the estimate would be reported
    # as a statement the amount no longer is.
    statement = session.get(InstanceStatement, tx.id)
    if statement is not None:
        before["statement"] = _statement_audit_shape(statement)
        session.delete(statement)
    if not in_rule or amount == 0 or reshaped:
        _void_and_release(session, tx, actor_token_id=token.id, reason="reset")
        if in_rule and amount != 0:
            materialize_schedule(session, schedule, actor_token_id=token.id)
    else:
        assert tx.occurrence_date is not None
        tx.date = tx.occurrence_date
        tx.description = schedule.name
        if schedule.counterparty_account_id is None:
            payee_name = _rule_payee_name(session, schedule)
            assert payee_name is not None  # a flat rule always names a payee
            tx.payee = ledger_service.resolve_payee(session, payee_name)
            category_leg = next(p for p in tx.postings if p.account.is_virtual)
            category_leg.account = _rule_category(session, schedule, amount)
        _set_instance_amount(tx, amount, schedule.account_id)
        session.flush()
    touch_instance(session, tx, pin=False)
    audit.record(
        session,
        actor_token_id=token.id,
        action="schedule.instance_reset",
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after=_instance_audit_shape(tx),
    )
    return tx


def list_instances(session: Session, schedule: Schedule) -> list[dict[str, Any]]:
    """Live instances for the detail surfaces, oldest first, each with the
    basis of its amount and any recorded card statement (decision 80)."""
    live = _live_instances(session, schedule)
    statements = statements_for(session, [tx.id for tx in live])
    out: list[dict[str, Any]] = []
    for tx in live:
        leg = next((p for p in tx.postings if p.account_id == schedule.account_id), None)
        statement = statements.get(tx.id)
        out.append(
            {
                "transaction_id": tx.id,
                "date": tx.date,
                "occurrence_date": tx.occurrence_date,
                "amount": Decimal(leg.amount) if leg is not None else None,
                "pinned": tx.schedule_pinned_at is not None,
                "overdue": tx.date < date_type.today(),
                "basis": instance_basis(schedule, tx, statement),
                "statement_balance": (
                    Decimal(statement.statement_balance) if statement is not None else None
                ),
                "statement_date": statement.statement_date if statement is not None else None,
                "statement_source": statement.source if statement is not None else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Card statement balances (#351, decision 80)
# ---------------------------------------------------------------------------


def statements_for(session: Session, tx_ids: list[uuid.UUID]) -> dict[uuid.UUID, InstanceStatement]:
    """The recorded statements of these transactions, by transaction id."""
    if not tx_ids:
        return {}
    return {
        row.transaction_id: row
        for row in session.execute(
            select(InstanceStatement).where(InstanceStatement.transaction_id.in_(tx_ids))
        ).scalars()
    }


def instance_basis(
    schedule: Schedule | None,
    tx: Transaction | None,
    statement: InstanceStatement | None,
) -> str:
    """Where an expected amount comes from, in the forecast's ``basis``
    vocabulary (``core_forecasting.AMOUNT_BASES``, decision 80):

    - ``statement``: a recorded card statement still sets the amount (one of
      the instance's legs equals the statement balance);
    - ``override``: the instance is pinned, so the amount is the user's — a
      statement overridden to another amount afterwards lands here too;
    - otherwise the rule's: ``fixed``, or the schedule's estimate basis.

    ``tx=None`` is a projected occurrence (no row yet); ``schedule=None`` an
    expected row no schedule owns, whose typed amount is ``fixed``."""
    if tx is not None:
        if statement is not None and any(
            Decimal(p.amount) == Decimal(statement.statement_balance) for p in tx.postings
        ):
            return core_forecasting.BASIS_STATEMENT
        if tx.schedule_pinned_at is not None:
            return core_forecasting.BASIS_OVERRIDE
    if schedule is None or schedule.amount_type == "fixed":
        return core_forecasting.BASIS_FIXED
    return schedule.estimate_basis or core_forecasting.EstimateBasis.ROLLING_AVG_3.value


def _statement_audit_shape(row: InstanceStatement) -> dict[str, Any]:
    return {
        "statement_balance": str(row.statement_balance),
        "statement_date": row.statement_date.isoformat() if row.statement_date else None,
        "source": row.source,
    }


@dataclass(frozen=True)
class RecordedStatement:
    """What ``record_statement_balance`` did: amounts are the schedule
    account's leg (the side the instance is listed and estimated from)."""

    tx: Transaction
    schedule: Schedule
    statement: InstanceStatement
    previous_amount: Decimal
    amount: Decimal


def _card_leg(tx: Transaction) -> Posting | None:
    """The leg a card payment pays INTO: a credit card's, positive."""
    return next(
        (p for p in tx.postings if p.account.type == "credit_card" and Decimal(p.amount) > 0),
        None,
    )


def _statement_figure(statement_balance: Decimal) -> Decimal:
    """The statement balance as stored, or LedgerError: positive (owed, as
    printed) and at most 4 decimal places — refused, never rounded."""
    try:
        if not statement_balance.is_finite() or abs(statement_balance) >= Decimal(10) ** 16:
            raise LedgerError("statement_balance is out of range")
        if statement_balance != statement_balance.quantize(core.QUANT):
            raise LedgerError("statement_balance has more than 4 decimal places")
    except InvalidOperation as exc:
        raise LedgerError("statement_balance is out of range") from exc
    if statement_balance <= 0:
        raise LedgerError(
            "statement_balance is the amount owed as the statement prints it, so it is "
            "positive; when nothing is owed there is no payment: skip the instance "
            "(skip_instance) instead"
        )
    return core.quantize(statement_balance)


def _statement_instance(session: Session, *, card: Account, due_date: date_type) -> Transaction:
    """The live payment instance to ``card`` due nearest ``due_date``, within
    ``STATEMENT_DUE_WINDOW_DAYS``: an expected transfer of any schedule whose
    card leg is positive. An equally near pair is refused, never guessed."""
    candidates = list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .join(Schedule, Schedule.id == Transaction.schedule_id)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.status == core.Status.EXPECTED.value,
                Transaction.superseded_by.is_(None),
                Schedule.counterparty_account_id.is_not(None),
                Posting.account_id == card.id,
                Posting.amount > 0,
            )
            .order_by(Transaction.date, Transaction.id)
        )
        .scalars()
        .unique()
    )
    if not candidates:
        raise LedgerError(
            f"{card.path} has no live payment instance to record a statement on; a payment "
            "schedule (a transfer to the card) materializes them"
        )
    near = [tx for tx in candidates if abs((tx.date - due_date).days) <= STATEMENT_DUE_WINDOW_DAYS]
    if not near:
        due = ", ".join(tx.date.isoformat() for tx in candidates[:5])
        raise LedgerError(
            f"no live payment to {card.path} is due within {STATEMENT_DUE_WINDOW_DAYS} days "
            f"of {due_date.isoformat()}; its live payments are due {due}"
        )
    best = min(abs((tx.date - due_date).days) for tx in near)
    tied = [tx for tx in near if abs((tx.date - due_date).days) == best]
    if len(tied) > 1:
        due = ", ".join(tx.date.isoformat() for tx in tied)
        raise LedgerError(
            f"{len(tied)} live payments to {card.path} are equally near "
            f"{due_date.isoformat()} ({due}); pass the due date of the one this statement "
            "is for"
        )
    return tied[0]


def record_statement_balance(
    session: Session,
    *,
    token: ApiToken,
    statement_balance: Decimal,
    statement_date: date_type | None = None,
    transaction_id: uuid.UUID | None = None,
    account_ref: str | None = None,
    due_date: date_type | None = None,
) -> RecordedStatement:
    """Record a card statement balance against the payment it is due on
    (write scope, decision 80). The instance is addressed by
    ``transaction_id``, or by the card (``account_ref``) plus the statement's
    ``due_date`` — the live payment instance nearest it, within
    ``STATEMENT_DUE_WINDOW_DAYS``. Credit cards only: a loan payment is an
    ordinary override.

    The amount moves through the ordinary transfer edit
    (``ledger.update_transaction`` with the card's leg = the balance, decision
    78), which pins the instance, marks it reviewed and audits
    ``transaction.update``; the statement row is upserted with its
    provenance and audited ``schedule.statement_balance``."""
    _require_write(token, "recording a statement balance")
    if (transaction_id is None) == (account_ref is None):
        raise LedgerError("address the payment by transaction_id, or by account plus due_date")
    if account_ref is not None and due_date is None:
        raise LedgerError("due_date is required with account: the statement's payment due date")
    if transaction_id is not None and due_date is not None:
        raise LedgerError(
            "due_date addresses a payment by account; leave it out with transaction_id"
        )
    figure = _statement_figure(statement_balance)
    if statement_date is not None and statement_date > date_type.today():
        raise LedgerError(f"statement_date {statement_date.isoformat()} is in the future")

    if account_ref is not None:
        assert due_date is not None
        card = ledger_service.resolve_account(session, account_ref)
        if card.is_virtual or card.type != "credit_card":
            raise LedgerError(
                f"{card.path} is not a credit card: statement balances are recorded on card "
                "payments; override a loan payment with override_instance"
            )
        tx = _statement_instance(session, card=card, due_date=due_date)
    else:
        assert transaction_id is not None
        tx = ledger_service.get_transaction(session, transaction_id)
    if (
        tx.schedule_id is None
        or tx.status != core.Status.EXPECTED.value
        or tx.superseded_by is not None
    ):
        raise LedgerError("not a live schedule instance")
    schedule = get_schedule(session, tx.schedule_id)
    card_leg = _card_leg(tx) if schedule.counterparty_account_id is not None else None
    if card_leg is None:
        raise LedgerError(
            "a statement balance is recorded only on a payment to a credit card; "
            "override this instance with override_instance"
        )
    if statement_date is not None and statement_date > tx.date:
        raise LedgerError(
            f"statement_date {statement_date.isoformat()} is after the payment's due date "
            f"{tx.date.isoformat()}"
        )

    def schedule_leg() -> Decimal:
        return next(Decimal(p.amount) for p in tx.postings if p.account_id == schedule.account_id)

    previous_amount = schedule_leg()
    prior = session.get(InstanceStatement, tx.id)
    before = _statement_audit_shape(prior) if prior is not None else None
    ledger_service.update_transaction(
        session, tx_id=tx.id, token=token, amount=figure, account_ref=str(card_leg.account_id)
    )
    row = prior if prior is not None else InstanceStatement(transaction_id=tx.id)
    row.statement_balance = figure
    row.statement_date = statement_date
    row.source = "human" if token.kind == "session" else "agent"
    row.recorded_by = token.id
    row.recorded_at = datetime.now(UTC)
    session.add(row)
    session.flush()
    amount = schedule_leg()
    audit.record(
        session,
        actor_token_id=token.id,
        action="schedule.statement_balance",
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after={
            **_statement_audit_shape(row),
            "previous_amount": str(previous_amount),
            "amount": str(amount),
        },
    )
    return RecordedStatement(
        tx=tx,
        schedule=schedule,
        statement=row,
        previous_amount=previous_amount,
        amount=amount,
    )


def _rule_category(session: Session, schedule: Schedule, amount: Decimal) -> Account:
    """The category leg a flat schedule's instance books to: its template,
    else the uncategorized placeholder for the amount's direction."""
    category_ref = (str(schedule.category_path) if schedule.category_path else None) or (
        "expenses.uncategorized" if amount < 0 else "income.uncategorized"
    )
    try:
        return ledger_service.resolve_account(session, category_ref)
    except NotFoundError:
        from purser.feed.importer import _uncategorized

        return _uncategorized(session, income=amount > 0)


def materialize_schedule(
    session: Session,
    schedule: Schedule,
    *,
    horizon_days: int = MATERIALIZE_HORIZON_DAYS,
    actor_token_id: uuid.UUID | None = None,
) -> int:
    """Create expected instances on the rolling horizon. Idempotent on
    (schedule, occurrence_date) across every status — a matched (voided)
    or skipped slot is never re-created; only a released slot (NULL) is."""
    if schedule.status != "active":
        return 0
    account = session.get(Account, schedule.account_id)
    assert account is not None
    if account.closed_at is not None:
        return 0  # a closed account accepts no new expected instances (DESIGN §3.4)
    today = date_type.today()
    due_dates = core_forecasting.expand_rrule(
        schedule.rrule,
        dtstart=schedule.dtstart,
        start=today,
        end=today + timedelta(days=horizon_days),
    )
    payee_name = _rule_payee_name(session, schedule)
    amount = _instance_amount(session, schedule)
    _refresh_estimates(session, schedule, amount, actor_token_id=actor_token_id)
    taken = {
        row
        for row in session.execute(
            select(Transaction.occurrence_date).where(
                Transaction.schedule_id == schedule.id,
                Transaction.occurrence_date.is_not(None),
            )
        ).scalars()
    }
    created = 0
    for due in due_dates:
        if due in taken:
            continue
        if amount == 0:
            continue  # estimated schedule with no history and no fallback yet
        if schedule.counterparty_account_id is not None:
            # Two-leg expected transfer (#232): amount is the schedule
            # account's leg, so a negative amount flows OUT to the counterparty.
            outgoing, incoming = (
                (account.id, schedule.counterparty_account_id)
                if amount < 0
                else (schedule.counterparty_account_id, account.id)
            )
            ledger_service.create_transfer(
                session,
                from_account_ref=str(outgoing),
                to_account_ref=str(incoming),
                amount=abs(amount),
                date=due,
                description=schedule.name,
                requested_status=core.Status.EXPECTED,
                origin=core.Origin.RULE,
                token=None,
                schedule_id=schedule.id,
                occurrence_date=due,
            )
            created += 1
            continue
        category = _rule_category(session, schedule, amount)
        ledger_service.create_flat_transaction(
            session,
            account_ref=str(account.id),
            category_ref=str(category.id),
            amount=amount,
            date=due,
            payee_name=payee_name,
            description=schedule.name,
            requested_status=core.Status.EXPECTED,
            origin=core.Origin.RULE,
            token=None,
            schedule_id=schedule.id,
            occurrence_date=due,
        )
        created += 1
    refresh_next_due(session, schedule)
    return created


def materialize_all(session: Session) -> dict[str, int]:
    schedules = session.execute(select(Schedule).where(Schedule.status == "active")).scalars()
    created = sum(materialize_schedule(session, s) for s in schedules)
    return {"instances_created": created}


def materialize_all_job() -> None:
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        materialize_all(session)
        session.commit()


# ---------------------------------------------------------------------------
# Recurrence detection over history (proposes, never activates)
# ---------------------------------------------------------------------------


LIABILITY_TYPES = ("credit_card", "loan")
DETECTOR = "recurrence_v2"
DETECTOR_VERSION = 2


@dataclass
class _Group:
    """One (account, counterparty) pattern candidate, seen through the
    account's leg (#234): every occurrence is that leg's amount."""

    account: Account
    counterparty: Account | None
    payee: Payee | None
    occurrences: list[core_recurrence.Occurrence] = field(default_factory=list)
    categories: Counter[str] = field(default_factory=Counter)

    @property
    def key(self) -> str:
        if self.counterparty is not None:
            return f"acct:{self.counterparty.id}"
        assert self.payee is not None
        return f"payee:{self.payee.name_bidx}"

    @property
    def display_name(self) -> str:
        if self.payee is not None:
            return self.payee.name
        assert self.counterparty is not None
        return self.counterparty.name


def _history_rows(session: Session, since: date_type) -> list[Transaction]:
    """Live bank-shaped history: pending/posted rows that are not schedule
    instances. Merged transfers (two real legs, any origin) ride along —
    the perspective-leg view is what makes them detectable (#232/#234)."""
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    return list(
        session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.status.in_(["pending", "posted"]),
                Transaction.superseded_by.is_(None),
                Transaction.schedule_id.is_(None),
                Transaction.id.not_in(reversed_ids),
                Transaction.date >= since,
            )
        ).scalars()
    )


def _group_history(
    session: Session,
    rows: list[Transaction],
    *,
    only_account_id: uuid.UUID | None,
    only_counterparty: Account | None,
) -> dict[tuple[uuid.UUID, str], _Group]:
    from purser import rules as rules_service

    groups: dict[tuple[uuid.UUID, str], _Group] = {}
    payee_cache: dict[uuid.UUID, Payee] = {}

    def resolved_payee(tx: Transaction) -> Payee | None:
        if tx.payee_id is None:
            return None
        target = rules_service.resolve_payee_id(session, tx.payee_id)
        assert target is not None
        if target not in payee_cache:
            row = session.get(Payee, target)
            assert row is not None
            payee_cache[target] = row
        return payee_cache[target]

    for tx in rows:
        real = [p for p in tx.postings if not p.account.is_virtual]
        virtual = [p for p in tx.postings if p.account.is_virtual]
        if len(real) == 2:
            payer = min(real, key=lambda p: Decimal(p.amount))
            other = next(p for p in real if p.id != payer.id)
            account, counterparty, payee = payer.account, other.account, None
            leg = payer
        elif len(real) == 1:
            leg = real[0]
            account = leg.account
            payee = resolved_payee(tx)
            counterparty = None
            if only_counterparty is not None:
                # The liability-added scan (#234): a one-sided OUTFLOW whose
                # payee names the new account is a payment to it that has not
                # been linked yet — treat it as a transfer to that counterparty.
                text = payee.name if payee is not None else (tx.description or "")
                if Decimal(leg.amount) >= 0 or not core_matching.mentions_counterparty(
                    text, only_counterparty.name, loose=True
                ):
                    continue
                counterparty, payee = only_counterparty, None
            elif payee is None:
                continue
        else:
            continue
        if only_account_id is not None and account.id != only_account_id:
            continue
        if only_counterparty is not None and (
            counterparty is None or counterparty.id != only_counterparty.id
        ):
            continue
        if counterparty is not None:
            group_key = (account.id, f"acct:{counterparty.id}")
        else:
            assert payee is not None
            group_key = (account.id, f"payee:{payee.name_bidx}")
        group = groups.get(group_key)
        if group is None:
            group = groups[group_key] = _Group(
                account=account, counterparty=counterparty, payee=payee
            )
        group.occurrences.append(
            core_recurrence.Occurrence(
                date=tx.date, amount=Decimal(leg.amount), transaction_id=tx.id
            )
        )
        if len(virtual) == 1:
            group.categories[str(virtual[0].account.path)] += 1
    return groups


def _draft_and_evidence(
    group: _Group, detected: core_recurrence.Detected
) -> tuple[Any, dict[str, Any]]:
    from purser import suggestions as suggestions_service

    if detected.amount_type == "estimated":
        basis: str | None = (
            core_forecasting.EstimateBasis.COUNTERPARTY_BALANCE.value
            if group.counterparty is not None and group.counterparty.type == "credit_card"
            else core_forecasting.EstimateBasis.ROLLING_AVG_3.value
        )
    else:
        basis = None
    category = group.categories.most_common(1)[0][0] if group.categories else None
    draft = suggestions_service.ScheduleDraft(
        name=group.display_name,
        account=group.account.path,
        counterparty_account=group.counterparty.path if group.counterparty else None,
        rrule=detected.rrule,
        dtstart=detected.dtstart,
        amount_type=detected.amount_type,
        amount=detected.typical_amount,
        estimate_basis=basis,
        payee=group.payee.name if group.payee else None,
        category=category if group.counterparty is None else None,
    )
    ordered = sorted(group.occurrences, key=lambda o: o.date)
    evidence = {
        "occurrences": [
            {
                "date": o.date.isoformat(),
                "amount": str(o.amount),
                "transaction_id": str(o.transaction_id) if o.transaction_id else None,
            }
            for o in ordered[-suggestions_service.EVIDENCE_OCCURRENCES_CAP :]
        ],
        "cadence": detected.cadence,
        "period_days": detected.period_days,
        "gaps": {
            "median_days": detected.gaps.median_days,
            "in_bucket": detected.gaps.in_bucket,
            "misses": detected.gaps.misses,
            "out_of_bucket": detected.gaps.out_of_bucket,
        },
        "amount": {
            "typical": str(detected.typical_amount),
            "low": str(detected.amount_low),
            "high": str(detected.amount_high),
            "cv": str(detected.amount_cv),
        },
    }
    return draft, evidence


def detect_schedules(
    session: Session,
    *,
    actor_token_id: uuid.UUID | None,
    account_ref: str | None = None,
    counterparty: Account | None = None,
    source: str = "detected",
) -> list[Suggestion]:
    """Recurring (account, counterparty) patterns in history become schedule
    SUGGESTIONS (#233/#234) — never schedules. One-sided rows group by
    alias-resolved payee, merged transfers by the other real account (proposed
    once, on the payer's side). Re-running refreshes open rows; rejected
    patterns and covered pairs are skipped by the fingerprint memory.
    ``counterparty`` narrows the scan to payments to that account (the
    liability-added hook), ``account_ref`` to one account."""
    from purser import suggestions as suggestions_service

    today = date_type.today()
    only_account = (
        ledger_service.resolve_account(session, account_ref) if account_ref is not None else None
    )
    rows = _history_rows(session, today - timedelta(days=DETECTION_LOOKBACK_DAYS))
    groups = _group_history(
        session,
        rows,
        only_account_id=only_account.id if only_account is not None else None,
        only_counterparty=counterparty,
    )
    touched: list[Suggestion] = []
    for (account_id, key), group in groups.items():
        fingerprint = suggestions_service.schedule_fingerprint(account_id, key)
        if suggestions_service.is_suppressed(
            session,
            fingerprint=fingerprint,
            account_id=account_id,
            counterparty_account_id=group.counterparty.id if group.counterparty else None,
            payee_id=group.payee.id if group.payee else None,
        ):
            continue
        detected = core_recurrence.detect(group.occurrences, today=today)
        if detected is None:
            continue
        draft, evidence = _draft_and_evidence(group, detected)
        row, _created = suggestions_service.upsert_schedule_suggestion(
            session,
            draft=draft,
            source=source,
            fingerprint=fingerprint,
            subject_account_id=account_id,
            subject_payee_id=group.payee.id if group.payee else None,
            counterparty_account_id=group.counterparty.id if group.counterparty else None,
            evidence=evidence,
            confidence=detected.confidence,
            detector=DETECTOR,
            detector_version=DETECTOR_VERSION,
            actor_token_id=actor_token_id,
        )
        touched.append(row)
    return touched


def detect_for_new_liability(
    session: Session, account: Account, *, actor_token_id: uuid.UUID | None
) -> list[Suggestion]:
    """The liability-added hook (#234): a new card or loan is a strong signal
    that payments to it already sit in the other accounts' history — merged
    transfers to it, or one-sided rows whose payee names it. Emits
    ``source='account_added'`` suggestions; never activates anything; quiet
    when the history is thin (the nightly detector catches it later)."""
    if account.is_virtual or account.type not in LIABILITY_TYPES:
        return []
    return detect_schedules(
        session, actor_token_id=actor_token_id, counterparty=account, source="account_added"
    )


def detect_job() -> None:
    """Nightly: after matching/auto-link (06:45) and materialize (07:00)."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        detect_schedules(session, actor_token_id=None)
        session.commit()


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


# The bases whose amount the rule estimates from history (#247): only these
# carry a range band. fixed, override and statement amounts are known.
_ESTIMATED_BASES = frozenset(b.value for b in core_forecasting.EstimateBasis)


def _cached_estimate(
    session: Session, schedule: Schedule, cache: dict[uuid.UUID, ScheduleEstimate]
) -> ScheduleEstimate:
    if schedule.id not in cache:
        cache[schedule.id] = schedule_estimate(session, schedule)
    return cache[schedule.id]


def _estimate_band(
    estimate: ScheduleEstimate, amount: Decimal, *, counterparty_side: bool
) -> tuple[Decimal, Decimal] | None:
    """The band of an estimated occurrence (#247, decision 85): its settled
    history plus ``amount`` (the schedule account's leg), seen from the
    forecast account — the counterparty of a transfer receives the negation,
    so its band is negated and its ends swapped."""
    band = core_forecasting.estimate_range(estimate.actuals, amount)
    if band is None or not counterparty_side:
        return band
    return -band[1], -band[0]


def _projected_items(
    session: Session,
    account: Account,
    *,
    start: date_type,
    end: date_type,
    estimates: dict[uuid.UUID, ScheduleEstimate] | None = None,
) -> tuple[list[core_forecasting.ExpectedItem], int]:
    """Rule occurrences beyond what is materialized (#235): every active
    schedule touching the account (either side of a transfer), expanded on the
    fly for slots in [start, end] that hold no instance of ANY status — a
    matched or skipped slot stays silent, a released one comes back. Returns
    the items and the count of active schedules touching the account. An
    estimated schedule's occurrences carry its range band (#247);
    ``estimates`` caches one estimate per schedule across the forecast."""
    cache: dict[uuid.UUID, ScheduleEstimate] = {} if estimates is None else estimates
    schedules = list(
        session.execute(
            select(Schedule).where(
                Schedule.status == "active",
                or_(
                    Schedule.account_id == account.id,
                    Schedule.counterparty_account_id == account.id,
                ),
            )
        ).scalars()
    )
    items: list[core_forecasting.ExpectedItem] = []
    for schedule in schedules:
        taken = {
            row
            for row in session.execute(
                select(Transaction.occurrence_date).where(
                    Transaction.schedule_id == schedule.id,
                    Transaction.occurrence_date.is_not(None),
                )
            ).scalars()
        }
        estimate = _cached_estimate(session, schedule, cache)
        if estimate.amount == 0:
            continue
        # The forecast sees the schedule through THIS account's leg: the
        # counterparty of a transfer schedule receives what the payer sends.
        counterparty_side = schedule.counterparty_account_id == account.id
        amount = -estimate.amount if counterparty_side else estimate.amount
        basis = instance_basis(schedule, None, None)
        band = (
            _estimate_band(estimate, estimate.amount, counterparty_side=counterparty_side)
            if basis in _ESTIMATED_BASES
            else None
        )
        for slot in core_forecasting.expand_rrule(
            schedule.rrule, dtstart=schedule.dtstart, start=start, end=end
        ):
            if slot in taken:
                continue
            items.append(
                core_forecasting.ExpectedItem(
                    date=slot,
                    amount=amount,
                    label=schedule.name,
                    projected=True,
                    basis=basis,
                    range=band,
                )
            )
    return items, len(schedules)


FORECAST_MAX_DAYS = 365


def validate_horizon(days: int) -> int:
    """The ONE forecast-horizon check (#354, decision 77): 1..365 days.

    ``forecast_balance``, the ``purser://forecast`` resource and
    ``forecast_account`` itself run it, and REST bounds ``days`` by the same
    constant, so every horizon the tool accepts is one the resource serves —
    a ``daily_uri`` it hands out always resolves (the #289 rule)."""
    if isinstance(days, bool) or not 1 <= days <= FORECAST_MAX_DAYS:
        raise LedgerError(f"days must be 1..{FORECAST_MAX_DAYS}, got {days}")
    return days


def forecast_account(
    session: Session, *, account_ref: str, horizon_days: int = 30
) -> dict[str, Any]:
    validate_horizon(horizon_days)
    account = ledger_service.resolve_account(session, account_ref)
    today = date_type.today()
    # Start where the register headline says the account is (decision 50):
    # the LIVE postings basis plus the valuation/holdings overlay. The basis
    # stays live rather than balance_asof(today) on purpose — a future-dated
    # pending row is not an `expected` row, so it never appears in the event
    # loop below; dropping it from the opening balance too would erase it
    # from the projection entirely.
    balance = networth_service.reported_balances(session, [account.id])[account.id].balance
    end = today + timedelta(days=horizon_days)
    rows = list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .options(selectinload(Transaction.postings), selectinload(Transaction.payee))
            .where(
                Transaction.status == "expected",
                Transaction.superseded_by.is_(None),
                Posting.account_id == account.id,
                Transaction.date <= end,
            )
            .distinct()
        ).scalars()
    )
    from purser import matching as matching_service

    # Settled pending confirmation (decision 76): an instance whose open
    # proposal names a live bank row on this account is already in the
    # opening balance through that row, so it stays out of the line — what
    # confirming the match would do — and is counted in coverage instead.
    settled = matching_service.proposed_settlements(
        session, account_id=account.id, expected_ids=[tx.id for tx in rows]
    )
    matches_pending = 0
    matches_pending_net = Decimal(0)
    # Each event says where its amount came from (decision 80).
    schedule_ids = {tx.schedule_id for tx in rows if tx.schedule_id is not None}
    owners = (
        {
            s.id: s
            for s in session.execute(
                select(Schedule).where(Schedule.id.in_(schedule_ids))
            ).scalars()
        }
        if schedule_ids
        else {}
    )
    statements = statements_for(session, [tx.id for tx in rows])
    # One estimate per schedule, shared with the projected occurrences (#247).
    estimates: dict[uuid.UUID, ScheduleEstimate] = {}
    items = []
    for tx in rows:
        leg = next((p for p in tx.postings if p.account_id == account.id), None)
        if leg is None:
            continue
        if tx.id in settled:
            matches_pending += 1
            matches_pending_net += Decimal(leg.amount)
            continue
        owner = owners.get(tx.schedule_id) if tx.schedule_id else None
        basis = instance_basis(owner, tx, statements.get(tx.id))
        band = None
        if owner is not None and basis in _ESTIMATED_BASES:
            own_leg = next((p for p in tx.postings if p.account_id == owner.account_id), None)
            if own_leg is not None:
                band = _estimate_band(
                    _cached_estimate(session, owner, estimates),
                    Decimal(own_leg.amount),
                    counterparty_side=owner.account_id != account.id,
                )
        items.append(
            core_forecasting.ExpectedItem(
                date=tx.date,
                amount=Decimal(leg.amount),
                label=tx.description or (tx.payee.name if tx.payee else "expected"),
                transaction_id=tx.id,
                basis=basis,
                range=band,
            )
        )
    instances = len(items)
    # Honour the horizon (#235): slots past MATERIALIZE_HORIZON_DAYS have no
    # rows yet, so the rules are expanded on the fly for them.
    projected, schedules_active = _projected_items(
        session, account, start=today, end=end, estimates=estimates
    )
    items.extend(projected)
    result = core_forecasting.forecast_balance(
        start_balance=balance,
        start=today,
        horizon_days=horizon_days,
        items=items,
        min_balance=Decimal(account.min_balance) if account.min_balance is not None else None,
    )
    from purser import suggestions as suggestions_service

    coverage: dict[str, Any] = {
        "schedules_active": schedules_active,
        "instances": instances,
        "projected": len(projected),
        "suggestions_open": suggestions_service.open_count(
            session, account_id=account.id, kind=suggestions_service.KIND_SCHEDULE
        ),
        "matches_pending": matches_pending,
    }
    # A Decimal, present only when nonzero: each surface serializes it the
    # way it serializes money (guardrail 1) and omits it otherwise.
    if matches_pending_net != 0:
        coverage["matches_pending_net"] = matches_pending_net
    return {
        "account_path": account.path,
        "account_id": account.id,
        "forecast": result,
        "coverage": coverage,
    }


def shortfall_scan_job() -> None:
    """Nightly projected-shortfall scan for accounts with a min_balance."""
    from purser.db import get_sessionmaker
    from purser.models import Event

    with get_sessionmaker()() as session:
        accounts = session.execute(
            select(Account).where(
                Account.min_balance.is_not(None),
                Account.is_virtual.is_(False),
                Account.closed_at.is_(None),  # closed accounts have no live forecast
            )
        ).scalars()
        for account in accounts:
            result = forecast_account(session, account_ref=str(account.id), horizon_days=30)
            forecast: core_forecasting.Forecast = result["forecast"]
            if forecast.first_shortfall is None:
                continue
            key = f"{account.id}:{forecast.first_shortfall.isoformat()}"
            exists = session.execute(
                select(Event.id).where(
                    Event.type == "forecast.shortfall",
                    Event.dedup_key == key,
                )
            ).first()
            if exists is None:
                events.emit(
                    session,
                    type_="forecast.shortfall",
                    entity_type="account",
                    entity_id=account.id,
                    payload={
                        "key": key,
                        "account": account.path,
                        "date": forecast.first_shortfall.isoformat(),
                        "low": str(min(d.balance for d in forecast.days)),
                        "min_balance": str(account.min_balance),
                    },
                    dedup_key=key,
                )
        session.commit()
