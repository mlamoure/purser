"""Suggestions — the inbox of things Purser thinks the user should do
(issue #233, DESIGN §6, decisions 57 and 67).

A suggestion is a *typed draft* of a target object plus the state the target
must not carry: evidence, confidence, a stable fingerprint, snooze, a scoped
rejection, and what accepting it produced. Two kinds, each with its own draft
type and its own apply:

- ``schedule`` — a :class:`ScheduleDraft`; accept creates the schedule
  (active, materialized).
- ``transfer_link`` (#271, decision 67) — a :class:`TransferLinkDraft`
  proposed by a manual ``confirm_transfer``; accept creates a payee-scoped
  ``transfer_link`` rule as a DRAFT, which becomes active only through the
  rules activation path and its projected-link-count confirm (decision 46).
- ``match`` (#246, decision 86) — a :class:`MatchDraft` MIRRORING an open
  match proposal (``matches.state = 'proposed'``). The ``matches`` row stays
  the source of truth: the ledger relies on a proposal holding its rows. The
  card adds snooze and rejection memory; accept confirms the proposal and
  reject rejects it. A card the system closes (the proposal was rescinded,
  carried, purged or auto-confirmed) is ``withdrawn``, never deleted.

Three rules keep the inbox from becoming noise:

- **One row per fingerprint, ever.** Re-detection (or a repeat confirm)
  upserts the open row (evidence/draft refreshed, ``seen_count`` bumped); a
  rejected row is durable suppression; an accepted row is skipped because
  what it produced now covers the pattern itself.
- **Accept is a deterministic apply, never a re-derivation.** The draft is
  validated by its kind's schema and handed to the target's create path
  unchanged (or with the caller's edits, validated by the same schema).
- **Nothing auto-accepts.** Snooze is a date; expiry brings the row back to
  the inbox, nothing more.

Scope mapping (guardrail 4): ``write`` creates suggestions when the caller
chooses to propose (``create_schedule(propose=True)``; a transfer confirm
also lands one, because a confirm only proposes the rule) and ``write``
decides them (#436). The UI card and the MCP tool are two renderings of
one row.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from purser import audit, events
from purser import ledger as ledger_service
from purser import rules as rules_service
from purser.admin import security
from purser.core import matching as core_matching
from purser.ledger import LedgerError, NotFoundError
from purser.models import (
    Account,
    ApiToken,
    Match,
    Payee,
    Posting,
    Rule,
    Schedule,
    Suggestion,
    Transaction,
)

KIND_SCHEDULE = "schedule"
KIND_TRANSFER_LINK = "transfer_link"
KIND_MATCH = "match"
KINDS = (KIND_SCHEDULE, KIND_TRANSFER_LINK, KIND_MATCH)
SOURCE_CONFIRM = "confirm"
SOURCE_DETECTED = "detected"
STATUS_OPEN = "open"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_WITHDRAWN = "withdrawn"
STATUSES = (STATUS_OPEN, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_WITHDRAWN)
MATCH_DETECTOR = "matcher"
DECISIONS = ("accept", "reject", "snooze", "edit_then_accept")
REJECT_SCOPES = ("exact", "counterparty")
DEFAULT_SNOOZE_DAYS = 30
LIST_MAX_ROWS = 25
EVIDENCE_OCCURRENCES_CAP = 12
# An agent's note on its proposal (#250): the evidence a person deciding it
# reads, such as the statement line the schedule came from.
AGENT_NOTE_MAX_CHARS = 200
CONFIRM_DETECTOR = "confirm_transfer"


class SuggestionError(LedgerError):
    pass


class SuggestionWithdrawn(SuggestionError):
    """Deciding a match card whose proposal is already gone: the card was
    marked ``withdrawn`` on the way out, and the caller commits that before
    answering 422 — the card must not stay in the inbox for another try."""


class ScheduleDraft(BaseModel):
    """The typed draft a schedule suggestion carries — exactly the arguments
    ``create_schedule`` takes, so accepting is a pure apply. Versioned so a
    future shape can migrate stored drafts instead of guessing at them."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    account: str
    counterparty_account: str | None = None
    rrule: str
    dtstart: date_type
    amount_type: Literal["fixed", "estimated"] = "fixed"
    amount: Decimal | None = None
    estimate_basis: (
        Literal["last", "rolling_avg_3", "rolling_avg_12", "counterparty_balance"] | None
    ) = None
    payee: str | None = None
    category: str | None = None
    autopost_policy: Literal["auto_post", "wait_for_match", "reminder_only"] = "wait_for_match"

    def stored(self) -> dict[str, Any]:
        """JSON-safe form for the encrypted column (money as strings)."""
        data = self.model_dump()
        data["dtstart"] = self.dtstart.isoformat()
        data["amount"] = str(self.amount) if self.amount is not None else None
        return data


# A strict whole number of days: 5.5, "5" and true are refused, not coerced —
# the same contract the rules API holds window_days to (decision 66).
WindowDays = Annotated[int, Field(strict=True, ge=0, le=core_matching.TRANSFER_WINDOW_MAX_DAYS)]


class TransferLinkDraft(BaseModel):
    """The typed draft a transfer_link suggestion carries (decision 67) —
    exactly the rule accepting it creates, as a DRAFT: the account pair a
    human confirmed, scoped to the confirm's payee and every alias of it.

    The pair and the payee are the suggestion's identity (its fingerprint),
    so only the narrowing fields are editable (:data:`TRANSFER_LINK_EDITABLE`).
    ``amount_exact``, ``window_days`` and ``effective_from`` start unset: one
    confirm says who the transfer is with, not how much or how far apart its
    legs post. Versioned like :class:`ScheduleDraft`."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    from_account: str
    to_account: str
    payee: str = Field(min_length=1)
    payee_id: uuid.UUID
    amount_exact: Decimal | None = None
    window_days: WindowDays | None = None
    effective_from: date_type | None = None

    @field_validator("amount_exact", mode="before")
    @classmethod
    def _no_float_money(cls, value: object) -> object:
        # Guardrail 1: a float never touches money — an amount arrives as a
        # decimal string (or an exact int), never a JSON float.
        if isinstance(value, float):
            raise ValueError("amount_exact must be a decimal string, not a float")
        return value

    def stored(self) -> dict[str, Any]:
        """JSON-safe form for the encrypted column (money as strings)."""
        data = self.model_dump()
        data["payee_id"] = str(self.payee_id)
        data["amount_exact"] = str(self.amount_exact) if self.amount_exact is not None else None
        data["effective_from"] = self.effective_from.isoformat() if self.effective_from else None
        return data


TRANSFER_LINK_EDITABLE = frozenset({"amount_exact", "window_days", "effective_from"})


class MatchDraft(BaseModel):
    """The typed draft a match card carries (decision 86): the proposal's
    two rows as the card shows them, money as decimal strings when stored.
    ``account`` is the account both rows were scored on (the expected's
    perspective leg), ``label`` the bill's payee or description, and the
    amounts are each row's leg there; ``variance`` is posted - expected.

    Nothing is editable: the pair IS the suggestion — picking another bank
    row is ``match``, not an edit. Versioned like :class:`ScheduleDraft`."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    expected_id: uuid.UUID
    posted_id: uuid.UUID
    account: str
    label: str
    expected_date: date_type
    expected_amount: Decimal
    posted_date: date_type
    posted_amount: Decimal
    variance: Decimal

    def stored(self) -> dict[str, Any]:
        """JSON-safe form for the encrypted column (money as strings)."""
        return {
            "version": self.version,
            "expected_id": str(self.expected_id),
            "posted_id": str(self.posted_id),
            "account": self.account,
            "label": self.label,
            "expected_date": self.expected_date.isoformat(),
            "expected_amount": str(self.expected_amount),
            "posted_date": self.posted_date.isoformat(),
            "posted_amount": str(self.posted_amount),
            "variance": str(self.variance),
        }


def schedule_fingerprint(account_id: uuid.UUID, counterparty_key: str) -> str:
    """Stable identity of a schedule suggestion: the account it lives on plus
    who it is with — ``acct:<id>`` for a transfer, ``payee:<blind index>``
    for a one-sided pattern. The same pattern found tomorrow lands on the
    same row; the same pattern rejected last week stays rejected."""
    return hashlib.sha256(f"schedule:{account_id}:{counterparty_key}".encode()).hexdigest()


def transfer_link_fingerprint(
    account_a: uuid.UUID, account_b: uuid.UUID, payee_id: uuid.UUID
) -> str:
    """Stable identity of a transfer_link suggestion: the UNORDERED account
    pair (the rule it drafts is unordered, so a confirm in either direction
    proposes the same rule) plus the scope payee, resolved through its aliases
    when the row was created. An alias made later is caught by
    :func:`_equivalent_rows`, which compares alias groups, not fingerprints."""
    low, high = sorted((str(account_a), str(account_b)))
    return hashlib.sha256(f"transfer_link:{low}:{high}:payee:{payee_id}".encode()).hexdigest()


def counterparty_key_for(
    *, counterparty_account_id: uuid.UUID | None, payee_bidx: str | None, name: str
) -> str:
    if counterparty_account_id is not None:
        return f"acct:{counterparty_account_id}"
    if payee_bidx is not None:
        return f"payee:{payee_bidx}"
    return f"name:{hashlib.sha256(name.lower().encode()).hexdigest()}"


def get_suggestion(session: Session, suggestion_id: uuid.UUID) -> Suggestion:
    row = session.get(Suggestion, suggestion_id)
    if row is None:
        raise NotFoundError(f"suggestion {suggestion_id} not found")
    return row


def existing_schedule_for(
    session: Session,
    *,
    account_id: uuid.UUID,
    counterparty_account_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
) -> Schedule | None:
    """The schedule already covering (account, counterparty) — any status but
    the ones that were never real — preferring a live one: active, then
    paused, then the newest ended."""
    stmt = select(Schedule).where(
        Schedule.account_id == account_id, Schedule.status.in_(["active", "paused", "ended"])
    )
    if counterparty_account_id is not None:
        stmt = stmt.where(Schedule.counterparty_account_id == counterparty_account_id)
    elif payee_id is not None:
        stmt = stmt.where(Schedule.payee_id == payee_id)
    else:
        return None
    live_first = case((Schedule.status == "active", 0), (Schedule.status == "paused", 1), else_=2)
    return session.execute(
        stmt.order_by(live_first, Schedule.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def schedule_exists_for(
    session: Session,
    *,
    account_id: uuid.UUID,
    counterparty_account_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
) -> bool:
    """A schedule already covering (account, counterparty) suppresses the
    suggestion: the user has already decided this pattern, one way or
    another."""
    return (
        existing_schedule_for(
            session,
            account_id=account_id,
            counterparty_account_id=counterparty_account_id,
            payee_id=payee_id,
        )
        is not None
    )


def agent_note(note: str | None) -> str | None:
    """An agent's proposal note, trimmed (blank means none), or
    SuggestionError past ``AGENT_NOTE_MAX_CHARS``."""
    if note is None or not note.strip():
        return None
    note = note.strip()
    if len(note) > AGENT_NOTE_MAX_CHARS:
        raise SuggestionError(f"note must be {AGENT_NOTE_MAX_CHARS} characters or fewer")
    return note


def is_suppressed(
    session: Session,
    *,
    fingerprint: str,
    account_id: uuid.UUID,
    counterparty_account_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
) -> bool:
    """Durable memory: a rejected/accepted row for this fingerprint, a
    counterparty-scoped rejection on the same (account, counterparty), or an
    existing schedule for the pair."""
    existing = session.execute(
        select(Suggestion.status).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if existing in (STATUS_REJECTED, STATUS_ACCEPTED):
        return True
    scoped = select(Suggestion.id).where(
        Suggestion.kind == KIND_SCHEDULE,
        Suggestion.status == STATUS_REJECTED,
        Suggestion.reject_scope == "counterparty",
        Suggestion.subject_account_id == account_id,
    )
    if counterparty_account_id is not None:
        scoped = scoped.where(Suggestion.counterparty_account_id == counterparty_account_id)
    elif payee_id is not None:
        scoped = scoped.where(Suggestion.subject_payee_id == payee_id)
    else:
        scoped = scoped.where(Suggestion.id.is_(None))  # nothing to scope on
    if session.execute(scoped.limit(1)).first() is not None:
        return True
    return schedule_exists_for(
        session,
        account_id=account_id,
        counterparty_account_id=counterparty_account_id,
        payee_id=payee_id,
    )


def _refresh(
    session: Session,
    row: Suggestion,
    *,
    draft: dict[str, Any],
    evidence: dict[str, Any] | None,
    confidence: Decimal | None,
    detector: str,
    detector_version: int,
    actor_token_id: uuid.UUID | None,
) -> None:
    """Re-detection of an OPEN row: new draft/evidence, one more sighting."""
    now = datetime.now(UTC)
    row.draft = draft
    row.evidence = evidence
    row.confidence = confidence
    row.detector = detector
    row.detector_version = detector_version
    row.seen_count = (row.seen_count or 1) + 1
    row.last_seen_at = now
    row.updated_at = now
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="suggestion.refresh",
        entity_type="suggestion",
        entity_id=row.id,
        after={"fingerprint": row.fingerprint, "seen_count": row.seen_count},
    )


def _create(
    session: Session,
    *,
    kind: str,
    draft: dict[str, Any],
    source: str,
    fingerprint: str,
    subject_account_id: uuid.UUID,
    subject_payee_id: uuid.UUID | None,
    counterparty_account_id: uuid.UUID | None,
    evidence: dict[str, Any] | None,
    confidence: Decimal | None,
    detector: str,
    detector_version: int,
    actor_token_id: uuid.UUID | None,
    event_payload: dict[str, Any],
) -> Suggestion:
    """A new open row, its audit row and its ``suggestion.created`` event."""
    now = datetime.now(UTC)
    row = Suggestion(
        kind=kind,
        status=STATUS_OPEN,
        fingerprint=fingerprint,
        source=source,
        detector=detector,
        detector_version=detector_version,
        confidence=confidence,
        subject_account_id=subject_account_id,
        subject_payee_id=subject_payee_id,
        counterparty_account_id=counterparty_account_id,
        draft=draft,
        evidence=evidence,
        seen_count=1,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="suggestion.create",
        entity_type="suggestion",
        entity_id=row.id,
        after={
            "kind": kind,
            "source": source,
            "fingerprint": fingerprint,
            "draft": draft,
            "confidence": str(confidence) if confidence is not None else None,
        },
    )
    events.emit(
        session,
        type_="suggestion.created",
        entity_type="suggestion",
        entity_id=row.id,
        payload={
            "kind": kind,
            "source": source,
            **event_payload,
            "confidence": str(confidence) if confidence is not None else None,
        },
        dedup_key=fingerprint,
    )
    return row


def upsert_schedule_suggestion(
    session: Session,
    *,
    draft: ScheduleDraft,
    source: str,
    fingerprint: str,
    subject_account_id: uuid.UUID,
    subject_payee_id: uuid.UUID | None,
    counterparty_account_id: uuid.UUID | None,
    evidence: dict[str, Any] | None,
    confidence: Decimal | None,
    detector: str,
    detector_version: int,
    actor_token_id: uuid.UUID | None,
) -> tuple[Suggestion, bool]:
    """Create the suggestion, or refresh the open row that already carries
    this fingerprint. Returns (row, created). Rejected/accepted rows are
    returned untouched — callers check :func:`is_suppressed` first when they
    want to skip the work entirely."""
    row = session.execute(
        select(Suggestion).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is not None:
        if row.status != STATUS_OPEN:
            return row, False
        _refresh(
            session,
            row,
            draft=draft.stored(),
            evidence=evidence,
            confidence=confidence,
            detector=detector,
            detector_version=detector_version,
            actor_token_id=actor_token_id,
        )
        return row, False
    row = _create(
        session,
        kind=KIND_SCHEDULE,
        draft=draft.stored(),
        source=source,
        fingerprint=fingerprint,
        subject_account_id=subject_account_id,
        subject_payee_id=subject_payee_id,
        counterparty_account_id=counterparty_account_id,
        evidence=evidence,
        confidence=confidence,
        detector=detector,
        detector_version=detector_version,
        actor_token_id=actor_token_id,
        event_payload={"name": draft.name, "account": draft.account},
    )
    return row, True


# --------------------------------------------------------------------------
# transfer_link — proposed by a manual confirm (#271, decision 67)
# --------------------------------------------------------------------------


def _real_leg(tx: Transaction) -> Posting | None:
    return next((p for p in tx.postings if not p.account.is_virtual), None)


def _pair_clause(column_a: Any, column_b: Any, a: uuid.UUID, b: uuid.UUID) -> Any:
    """(column_a, column_b) is {a, b} in either order — the pair is unordered."""
    return or_(and_(column_a == a, column_b == b), and_(column_a == b, column_b == a))


def _pair_rows(session: Session, a: uuid.UUID, b: uuid.UUID) -> list[Suggestion]:
    return list(
        session.execute(
            select(Suggestion).where(
                Suggestion.kind == KIND_TRANSFER_LINK,
                _pair_clause(
                    Suggestion.subject_account_id, Suggestion.counterparty_account_id, a, b
                ),
            )
        ).scalars()
    )


def _equivalent_rows(
    session: Session, rows: list[Suggestion], payee_root: uuid.UUID, fingerprint: str
) -> list[Suggestion]:
    """The rows for this pair that name the same payee — compared by ALIAS
    GROUP at read time, so a rename that aliased the payee after a row was
    created (and changed what its fingerprint would be today) still finds
    it: a repeat confirm refreshes that row, a rejection of it still
    holds."""
    return [
        row
        for row in rows
        if row.fingerprint == fingerprint
        or (
            row.subject_payee_id is not None
            and rules_service.resolve_payee_id(session, row.subject_payee_id) == payee_root
        )
    ]


def _rule_covers(session: Session, a: uuid.UUID, b: uuid.UUID, payee_root: uuid.UUID) -> bool:
    """A transfer_link rule on this pair already says what the suggestion
    would — the whole pair, or a payee scope in the same alias group. A
    draft is under review and a disabled one is the user's call about this
    pair, so both count, like an active one; an archived rule is a deleted
    one and does not (a suggestion the user accepted still suppresses
    through its own row)."""
    rows = session.execute(
        select(Rule.payee_scope_id).where(
            Rule.type == rules_service.TYPE_TRANSFER_LINK,
            Rule.state != rules_service.STATE_ARCHIVED,
            _pair_clause(Rule.pair_account_a, Rule.pair_account_b, a, b),
        )
    ).scalars()
    return any(
        scope is None or rules_service.resolve_payee_id(session, scope) == payee_root
        for scope in rows
    )


def propose_transfer_link(
    session: Session,
    *,
    outgoing: Transaction,
    incoming: Transaction,
    merged: Transaction,
    token: ApiToken,
) -> Suggestion | None:
    """After a manual confirm merged ``outgoing``/``incoming`` into
    ``merged``: upsert the ONE transfer_link suggestion for this account pair
    and payee, or return None when there is nothing to propose.

    Called by ``matching.confirm_transfer`` for an ordinary (non-split)
    confirm. It never links anything and never writes a rule: the rule is a
    draft only once a human accepts the suggestion, and active only once they
    confirm its projected link count. So an unlink of the confirmed pair has
    nothing to unlearn — the only trace of the confirm is this visible,
    rejectable row (the #253 review's objection to a hidden memory).

    Nothing is proposed when:

    - neither row carries a payee — a whole-pair rule is a broader claim than
      one confirm justifies;
    - a transfer_link rule (draft, active or disabled) already covers the
      pair for this payee;
    - the pair was rejected with ``reject_scope='counterparty'``, or this
      pair and payee (alias group) were rejected or already accepted.

    The scope payee is the outgoing row's, else the incoming one's, resolved
    through its aliases; the evidence is the confirmed pairs (dates, amount,
    the merged transfer), capped like detection evidence.
    """
    out_leg, in_leg = _real_leg(outgoing), _real_leg(incoming)
    if out_leg is None or in_leg is None:
        return None
    payee_id = outgoing.payee_id if outgoing.payee_id is not None else incoming.payee_id
    if payee_id is None:
        return None
    payee_root = rules_service.resolve_payee_id(session, payee_id)
    assert payee_root is not None
    payee = session.get(Payee, payee_root)
    if payee is None:
        return None
    from_account, to_account = out_leg.account, in_leg.account
    a, b = from_account.id, to_account.id
    if _rule_covers(session, a, b, payee_root):
        return None
    rows = _pair_rows(session, a, b)
    if any(r.status == STATUS_REJECTED and r.reject_scope == "counterparty" for r in rows):
        return None
    fingerprint = transfer_link_fingerprint(a, b, payee_root)
    same = _equivalent_rows(session, rows, payee_root, fingerprint)
    if any(r.status != STATUS_OPEN for r in same):
        return None  # rejected (remembered) or accepted (its rule covers it)

    occurrence = {
        "date": outgoing.date.isoformat(),
        "incoming_date": incoming.date.isoformat(),
        "amount": str(abs(Decimal(out_leg.amount))),
        "transaction_id": str(merged.id),
        "outgoing_id": str(outgoing.id),
        "incoming_id": str(incoming.id),
    }
    open_row = next((r for r in same if r.status == STATUS_OPEN), None)
    if open_row is not None:
        occurrences = [
            o
            for o in (open_row.evidence or {}).get("occurrences", [])
            if o.get("transaction_id") != occurrence["transaction_id"]
        ]
        occurrences.append(occurrence)
        # Date order, as detection evidence is: an older pair confirmed later
        # still reads as the first, and the cap keeps the most recent pairs.
        occurrences.sort(key=lambda o: (o["date"], o["transaction_id"]))
        # The pair and the user's direction stay as first seen; the payee
        # follows its aliases, so the card names the payee as it is today.
        draft = {**open_row.draft, "payee": payee.name, "payee_id": str(payee_root)}
        _refresh(
            session,
            open_row,
            draft=TransferLinkDraft.model_validate(draft).stored(),
            evidence={"occurrences": occurrences[-EVIDENCE_OCCURRENCES_CAP:]},
            confidence=None,
            detector=CONFIRM_DETECTOR,
            detector_version=1,
            actor_token_id=token.id,
        )
        return open_row

    draft_model = TransferLinkDraft(
        from_account=from_account.path,
        to_account=to_account.path,
        payee=payee.name,
        payee_id=payee_root,
    )
    return _create(
        session,
        kind=KIND_TRANSFER_LINK,
        draft=draft_model.stored(),
        source=SOURCE_CONFIRM,
        fingerprint=fingerprint,
        subject_account_id=a,
        subject_payee_id=payee_root,
        counterparty_account_id=b,
        evidence={"occurrences": [occurrence]},
        confidence=None,
        detector=CONFIRM_DETECTOR,
        detector_version=1,
        actor_token_id=token.id,
        event_payload={
            "account": from_account.path,
            "counterparty_account": to_account.path,
            "payee": payee.name,
        },
    )


def draft_from_create_args(
    session: Session,
    *,
    name: str,
    account_ref: str,
    rrule: str,
    dtstart: date_type | None,
    amount_type: str,
    amount: Decimal | None,
    estimate_basis: str | None,
    payee_name: str | None,
    category_path: str | None,
    autopost_policy: str,
    counterparty_account_ref: str | None,
) -> tuple[ScheduleDraft, Account, Account | None]:
    """The ``propose=True`` path of ``create_schedule``: the same validation
    the direct path runs, captured as a draft instead of a row."""
    account = ledger_service.resolve_account(session, account_ref)
    counterparty = (
        ledger_service.resolve_account(session, counterparty_account_ref)
        if counterparty_account_ref
        else None
    )
    category = ledger_service.resolve_account(session, category_path) if category_path else None
    try:
        draft = ScheduleDraft(
            name=name,
            account=account.path,
            counterparty_account=counterparty.path if counterparty else None,
            rrule=rrule,
            dtstart=dtstart or date_type.today(),
            amount_type=amount_type,
            amount=amount,
            estimate_basis=estimate_basis,
            payee=payee_name,
            category=category.path if category else None,
            autopost_policy=autopost_policy,
        )
    except ValidationError as exc:
        raise SuggestionError(f"invalid schedule draft: {exc.errors()[0]['msg']}") from exc
    return draft, account, counterparty


# --------------------------------------------------------------------------
# match — the inbox mirror of a match proposal (#246, decision 86)
# --------------------------------------------------------------------------


def _match_slot_prefix(expected: Transaction) -> str:
    """Everything in a match fingerprint but the bank row. A schedule
    instance is named by its SLOT — schedule and occurrence — because an
    unmatch clone keeps the slot, so a rejection outlives the clone; any
    other expected by its id."""
    if expected.schedule_id is not None and expected.occurrence_date is not None:
        return f"match:slot:{expected.schedule_id}:{expected.occurrence_date.isoformat()}:"
    return f"match:tx:{expected.id}:"


def match_fingerprint(expected: Transaction, posted_id: uuid.UUID) -> str:
    """Stable identity of a match card, in plain text (ids and a date): the
    expected's slot (or id) plus the bank row. The same pair proposed again
    lands on the same row; the same pair rejected stays rejected."""
    return f"{_match_slot_prefix(expected)}{posted_id}"


def rejected_bank_rows(session: Session, expected: Transaction) -> set[uuid.UUID]:
    """The bank rows a person rejected for this expected's slot (or id): the
    rejection memory the matcher's tiers honour (``matching._best_for_expected``)."""
    prefix = _match_slot_prefix(expected)
    fingerprints = session.execute(
        select(Suggestion.fingerprint).where(
            Suggestion.kind == KIND_MATCH,
            Suggestion.status == STATUS_REJECTED,
            Suggestion.fingerprint.startswith(prefix, autoescape=True),
        )
    ).scalars()
    return {uuid.UUID(fp[len(prefix) :]) for fp in fingerprints}


def _match_rows(session: Session, match: Match) -> tuple[Transaction, Transaction] | None:
    from sqlalchemy.orm import selectinload

    rows = {
        t.id: t
        for t in session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(Transaction.id.in_([match.expected_id, match.posted_id]))
        ).scalars()
    }
    expected, posted = rows.get(match.expected_id), rows.get(match.posted_id)
    if expected is None or posted is None:
        return None
    return expected, posted


def _actionable(match: Match, expected: Transaction, posted: Transaction) -> bool:
    """A proposal confirming would act on: still proposed, its expected still
    open, its bank row still live."""
    from purser import matching

    return (
        match.state == "proposed"
        and expected.status == "expected"
        and expected.superseded_by is None
        and posted.status in matching.MATCHABLE_STATUSES
        and posted.superseded_by is None
    )


def _card_worthy(match: Match, expected: Transaction, posted: Transaction) -> bool:
    """An actionable proposal earns a card — except a bank row held for an
    expected TRANSFER (decision 65) while its other leg can still arrive: the
    merge usually settles it on its own, and the card would vanish a day
    later. The sweep surfaces it once that window has passed."""
    from purser import matching

    if not _actionable(match, expected, posted):
        return False
    window = timedelta(days=core_matching.TRANSFER_WINDOW_DAYS)
    held = matching._hold_target(posted, expected) is not None
    return not (held and posted.date >= date_type.today() - window)


def _match_card(
    match: Match, expected: Transaction, posted: Transaction
) -> tuple[MatchDraft, dict[str, Any], Decimal, uuid.UUID, uuid.UUID | None] | None:
    """(draft, evidence, confidence, subject account, counterparty account)."""
    real = [p for p in expected.postings if not p.account.is_virtual]
    on_posted = {p.account_id: p for p in posted.postings if not p.account.is_virtual}
    shared = [p for p in real if p.account_id in on_posted]
    if not shared:
        return None
    leg = min(shared, key=lambda p: Decimal(p.amount))  # the payer's view first
    posted_leg = on_posted[leg.account_id]
    other = next((p for p in real if p.id != leg.id), None) if len(real) == 2 else None
    expected_amount = Decimal(leg.amount)
    posted_amount = Decimal(posted_leg.amount)
    variance = (
        Decimal(match.variance) if match.variance is not None else posted_amount - expected_amount
    )
    label = (expected.payee.name if expected.payee else None) or expected.description or ""
    draft = MatchDraft(
        expected_id=expected.id,
        posted_id=posted.id,
        account=leg.account.path,
        label=label or leg.account.name,
        expected_date=expected.date,
        expected_amount=expected_amount,
        posted_date=posted.date,
        posted_amount=posted_amount,
        variance=variance,
    )
    evidence = {
        "occurrences": [
            {
                "date": posted.date.isoformat(),
                "amount": str(posted_amount),
                "transaction_id": str(posted.id),
                "description": posted.description,
            }
        ]
    }
    confidence = min(Decimal(1), max(Decimal(0), Decimal(str(match.score)))).quantize(
        Decimal("0.001")
    )
    return (
        draft,
        evidence,
        confidence,
        leg.account_id,
        other.account_id if other is not None else None,
    )


def upsert_match_suggestion(
    session: Session, match: Match, *, actor_token_id: uuid.UUID | None = None
) -> Suggestion | None:
    """The card for an open proposal: created, re-opened (a withdrawn or
    accepted card whose pair is proposed again), or quietly refreshed when
    what it shows changed. None when the proposal earns no card, or the pair
    was rejected (the memory stands; the matcher should not have proposed
    it)."""
    loaded = _match_rows(session, match)
    if loaded is None:
        return None
    expected, posted = loaded
    if not _card_worthy(match, expected, posted):
        return None
    parts = _match_card(match, expected, posted)
    if parts is None:
        return None
    draft, evidence, confidence, subject_account_id, counterparty_account_id = parts
    fingerprint = match_fingerprint(expected, posted.id)
    stored = draft.stored()
    row = session.execute(
        select(Suggestion).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is None:
        return _create(
            session,
            kind=KIND_MATCH,
            draft=stored,
            source=SOURCE_DETECTED,
            fingerprint=fingerprint,
            subject_account_id=subject_account_id,
            subject_payee_id=expected.payee_id,
            counterparty_account_id=counterparty_account_id,
            evidence=evidence,
            confidence=confidence,
            detector=MATCH_DETECTOR,
            detector_version=1,
            actor_token_id=actor_token_id,
            event_payload={
                "account": draft.account,
                "label": draft.label,
                "expected_date": draft.expected_date.isoformat(),
                "posted_amount": str(draft.posted_amount),
            },
        )
    if row.status == STATUS_REJECTED:
        return None
    if row.status == STATUS_OPEN:
        if row.draft != stored or row.confidence != confidence:
            row.draft = stored
            row.evidence = evidence
            row.confidence = confidence
            row.updated_at = datetime.now(UTC)
            session.flush()
            audit.record(
                session,
                actor_token_id=actor_token_id,
                action="suggestion.refresh",
                entity_type="suggestion",
                entity_id=row.id,
                after={"fingerprint": fingerprint, "seen_count": row.seen_count},
            )
        return row
    reopened_from = row.status
    now = datetime.now(UTC)
    row.status = STATUS_OPEN
    row.draft = stored
    row.evidence = evidence
    row.confidence = confidence
    row.subject_account_id = subject_account_id
    row.subject_payee_id = expected.payee_id
    row.counterparty_account_id = counterparty_account_id
    row.snooze_until = None
    row.decided_at = None
    row.decided_by = None
    row.decision_note = None
    row.reject_scope = None
    row.result_entity_type = None
    row.result_entity_id = None
    row.seen_count = (row.seen_count or 1) + 1
    row.last_seen_at = now
    row.updated_at = now
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="suggestion.refresh",
        entity_type="suggestion",
        entity_id=row.id,
        after={
            "fingerprint": fingerprint,
            "seen_count": row.seen_count,
            "reopened_from": reopened_from,
        },
    )
    return row


def _close_match_card(
    session: Session,
    row: Suggestion,
    *,
    status: str,
    match: Match,
    actor_token_id: uuid.UUID | None,
    reason: str,
) -> None:
    """Close a match card because its proposal closed outside the card: a
    token's confirm or reject in the register (or ``match``), or a system
    action — which withdraws it."""
    before = {"status": row.status}
    now = datetime.now(UTC)
    row.status = status
    row.decided_at = now
    row.decided_by = actor_token_id if status != STATUS_WITHDRAWN else None
    if status == STATUS_REJECTED:
        row.reject_scope = "exact"
    if status == STATUS_ACCEPTED:
        row.result_entity_type = "match"
        row.result_entity_id = match.id
    row.updated_at = now
    session.flush()
    if status == STATUS_WITHDRAWN:
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="suggestion.withdraw",
            entity_type="suggestion",
            entity_id=row.id,
            before=before,
            after={"fingerprint": row.fingerprint, "match_id": str(match.id), "reason": reason},
        )
        return
    decision = "accept" if status == STATUS_ACCEPTED else "reject"
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="suggestion.decide",
        entity_type="suggestion",
        entity_id=row.id,
        before=before,
        after={
            "decision": decision,
            "status": row.status,
            "reject_scope": row.reject_scope,
            "via": reason,
            "result_entity_id": str(row.result_entity_id) if row.result_entity_id else None,
        },
    )
    events.emit(
        session,
        type_="suggestion.decided",
        entity_type="suggestion",
        entity_id=row.id,
        payload={
            "kind": row.kind,
            "decision": decision,
            "status": row.status,
            "result_entity_type": row.result_entity_type,
            "result_entity_id": str(row.result_entity_id) if row.result_entity_id else None,
        },
    )


def on_proposal_closed(
    session: Session,
    match: Match,
    *,
    outcome: str,
    actor_token_id: uuid.UUID | None,
    reason: str,
    via_suggestion: uuid.UUID | None = None,
) -> None:
    """The hook every path that moves a match off ``proposed`` calls
    (decision 86): a token's confirm makes its card ``accepted``, a token's
    reject ``rejected`` (exact — the memory that stops the nightly pass
    proposing the pair again), and any system action ``withdrawn``.
    ``via_suggestion`` is the card being decided right now, which
    ``decide`` closes itself.

    A rejection is remembered even when the proposal never had a card (a
    held transfer leg, or a proposal made before the sweep ran): the card is
    written straight to ``rejected``."""
    if outcome not in (STATUS_ACCEPTED, STATUS_REJECTED, STATUS_WITHDRAWN):
        raise ValueError(f"unknown proposal outcome {outcome}")
    loaded = _match_rows(session, match)
    if loaded is None:
        return
    expected, posted = loaded
    fingerprint = match_fingerprint(expected, posted.id)
    row = session.execute(
        select(Suggestion).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is not None and row.id == via_suggestion:
        return
    if outcome == STATUS_REJECTED:
        if row is None:
            parts = _match_card(match, expected, posted)
            if parts is None:
                return
            draft, evidence, confidence, subject_account_id, counterparty_account_id = parts
            row = _create(
                session,
                kind=KIND_MATCH,
                draft=draft.stored(),
                source=SOURCE_DETECTED,
                fingerprint=fingerprint,
                subject_account_id=subject_account_id,
                subject_payee_id=expected.payee_id,
                counterparty_account_id=counterparty_account_id,
                evidence=evidence,
                confidence=confidence,
                detector=MATCH_DETECTOR,
                detector_version=1,
                actor_token_id=actor_token_id,
                event_payload={
                    "account": draft.account,
                    "label": draft.label,
                    "expected_date": draft.expected_date.isoformat(),
                    "posted_amount": str(draft.posted_amount),
                },
            )
        if row.status == STATUS_REJECTED:
            return
    elif row is None or row.status != STATUS_OPEN:
        return
    _close_match_card(
        session, row, status=outcome, match=match, actor_token_id=actor_token_id, reason=reason
    )


def sync_match_suggestions(
    session: Session, *, actor_token_id: uuid.UUID | None = None
) -> dict[str, int]:
    """The sweep at the end of every matching pass (decision 86): a card for
    every live, card-worthy proposal — which backfills the proposals made
    before cards existed, and surfaces a held transfer leg once its window
    passes — and ``withdrawn`` for every open card whose proposal is gone or
    no longer actionable (its expected was skipped, its bank row voided)."""
    stats = {"created": 0, "withdrawn": 0}
    live: set[str] = set()
    for match in session.execute(
        select(Match).where(Match.state == "proposed").order_by(Match.created_at, Match.id)
    ).scalars():
        loaded = _match_rows(session, match)
        if loaded is None or not _card_worthy(match, *loaded):
            continue
        fingerprint = match_fingerprint(loaded[0], match.posted_id)
        existing = session.execute(
            select(Suggestion.status).where(Suggestion.fingerprint == fingerprint)
        ).scalar_one_or_none()
        row = upsert_match_suggestion(session, match, actor_token_id=actor_token_id)
        if row is not None and row.status == STATUS_OPEN:
            live.add(fingerprint)
            if existing != STATUS_OPEN:
                stats["created"] += 1
    stale = session.execute(
        select(Suggestion).where(
            Suggestion.kind == KIND_MATCH,
            Suggestion.status == STATUS_OPEN,
            Suggestion.fingerprint.not_in(live) if live else Suggestion.id.is_not(None),
        )
    ).scalars()
    for row in list(stale):
        _withdraw_orphan(session, row, actor_token_id=actor_token_id)
        stats["withdrawn"] += 1
    return stats


def _withdraw_orphan(
    session: Session, row: Suggestion, *, actor_token_id: uuid.UUID | None, reason: str = "sweep"
) -> None:
    """Withdraw a match card whose proposal is gone (the sweep, or a stale
    decide)."""
    before = {"status": row.status}
    now = datetime.now(UTC)
    row.status = STATUS_WITHDRAWN
    row.decided_at = now
    row.decided_by = None
    row.updated_at = now
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="suggestion.withdraw",
        entity_type="suggestion",
        entity_id=row.id,
        before=before,
        after={"fingerprint": row.fingerprint, "reason": reason},
    )


def _live_proposal_for(session: Session, row: Suggestion) -> Match | None:
    """The open proposal a match card mirrors, while confirming it would
    still act on something."""
    draft = MatchDraft.model_validate(row.draft)
    match = session.execute(
        select(Match).where(
            Match.expected_id == draft.expected_id,
            Match.posted_id == draft.posted_id,
            Match.state == "proposed",
        )
    ).scalar_one_or_none()
    if match is None:
        return None
    loaded = _match_rows(session, match)
    return match if loaded is not None and _actionable(match, *loaded) else None


# --------------------------------------------------------------------------
# Deciding — dispatched on kind, a typed draft per kind
# --------------------------------------------------------------------------


def _apply_draft(
    session: Session, draft: ScheduleDraft, *, token: ApiToken, source: str
) -> Schedule:
    from purser import schedules as schedules_service

    created = schedules_service.create_schedule(
        session,
        name=draft.name,
        account_ref=draft.account,
        rrule=draft.rrule,
        amount_type=draft.amount_type,
        amount=draft.amount,
        token=token,
        payee_name=draft.payee,
        category_path=draft.category,
        estimate_basis=draft.estimate_basis,
        autopost_policy=draft.autopost_policy,
        source=source,
        dtstart=draft.dtstart,
        counterparty_account_ref=draft.counterparty_account,
    )
    assert isinstance(created, Schedule)  # a commit token never yields a suggestion
    return created


def _apply_transfer_link_draft(
    session: Session, draft: TransferLinkDraft, *, token: ApiToken, suggestion_id: uuid.UUID
) -> Rule:
    """The rule the draft describes, created as a DRAFT — never active: a
    transfer_link rule is retroactive over all posted history and merging is
    balance-neutral, so it activates only through ``rules.set_state`` with
    its projected link count confirmed (decision 46)."""
    pair_a = ledger_service.resolve_account(session, draft.from_account)
    pair_b = ledger_service.resolve_account(session, draft.to_account)
    fields: dict[str, object] = {
        "type": rules_service.TYPE_TRANSFER_LINK,
        "pair_account_a": pair_a.id,
        "pair_account_b": pair_b.id,
        "payee_scope_id": draft.payee_id,
        "note": f"Accepted from transfer-link suggestion {suggestion_id}",
    }
    if draft.amount_exact is not None:
        fields["amount_exact"] = draft.amount_exact
    if draft.window_days is not None:
        fields["window_days"] = draft.window_days
    if draft.effective_from is not None:
        fields["effective_from"] = draft.effective_from
    try:
        return rules_service.create(session, fields=fields, token=token, activate=False)
    except rules_service.RuleError as exc:
        raise SuggestionError(f"invalid transfer_link draft: {exc}") from exc


def _edited_draft(
    row: Suggestion, decision: str, edits: dict[str, Any] | None, editable: frozenset[str]
) -> dict[str, Any]:
    stored = dict(row.draft)
    if decision != "edit_then_accept":
        return stored
    if not edits:
        raise SuggestionError("edit_then_accept needs edits")
    unknown = set(edits) - editable
    if unknown:
        hint = (
            f" (a transfer_link suggestion edits only {', '.join(sorted(editable))};"
            " its pair and payee are what it proposes)"
            if row.kind == KIND_TRANSFER_LINK
            else ""
        )
        raise SuggestionError(f"unknown draft fields: {', '.join(sorted(unknown))}{hint}")
    stored.update(edits)
    return stored


def _accept_schedule(
    session: Session,
    row: Suggestion,
    *,
    decision: str,
    edits: dict[str, Any] | None,
    token: ApiToken,
) -> tuple[Schedule, dict[str, Any]]:
    stored = _edited_draft(row, decision, edits, frozenset(ScheduleDraft.model_fields))
    try:
        draft = ScheduleDraft.model_validate(stored)
    except ValidationError as exc:
        raise SuggestionError(f"invalid schedule draft: {exc.errors()[0]['msg']}") from exc
    created = _apply_draft(
        session, draft, token=token, source="detected" if row.source != "agent" else "agent"
    )
    return created, draft.stored()


def _accept_transfer_link(
    session: Session,
    row: Suggestion,
    *,
    decision: str,
    edits: dict[str, Any] | None,
    token: ApiToken,
) -> tuple[Rule, dict[str, Any]]:
    stored = _edited_draft(row, decision, edits, TRANSFER_LINK_EDITABLE)
    try:
        draft = TransferLinkDraft.model_validate(stored)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"])
        raise SuggestionError(f"invalid transfer_link draft: {where}: {error['msg']}") from exc
    created = _apply_transfer_link_draft(session, draft, token=token, suggestion_id=row.id)
    return created, draft.stored()


def decide(
    session: Session,
    *,
    suggestion_id: uuid.UUID,
    decision: str,
    token: ApiToken,
    edits: dict[str, Any] | None = None,
    note: str | None = None,
    reject_scope: str = "exact",
    snooze_until: date_type | None = None,
) -> tuple[Suggestion, Schedule | Rule | Match | None]:
    """accept | edit_then_accept -> what the draft produces: an active
    schedule (``schedule``), a DRAFT transfer_link rule awaiting its
    activation confirm (``transfer_link``), or the confirmed match
    (``match``); reject -> durable, scoped (a match card rejects exactly, and
    rejects its proposal); snooze -> back in the inbox on the date (a match
    card's proposal stays live and keeps holding its bank row). Write scope
    for every decision: each one changes what Purser expects or links."""
    if not security.can_write(token.scopes):
        raise SuggestionError("deciding a suggestion requires write scope")
    if decision not in DECISIONS:
        raise SuggestionError(f"decision must be one of {', '.join(DECISIONS)}")
    row = get_suggestion(session, suggestion_id)
    if row.status != STATUS_OPEN:
        raise SuggestionError(f"suggestion is already {row.status}")
    before = {
        "status": row.status,
        "snooze_until": (row.snooze_until.isoformat() if row.snooze_until else None),
    }
    now = datetime.now(UTC)
    created: Schedule | Rule | Match | None = None

    if row.kind == KIND_MATCH and decision != "snooze":
        if decision == "edit_then_accept":
            raise SuggestionError(
                "a match suggestion has nothing to edit — use match to pick another row"
            )
        if decision == "reject" and reject_scope != "exact":
            raise SuggestionError(
                "a match suggestion is rejected exactly (reject_scope=exact): it names one pair"
            )
        proposal = _live_proposal_for(session, row)
        if proposal is None:
            _withdraw_orphan(session, row, actor_token_id=token.id, reason="no_longer_proposed")
            raise SuggestionWithdrawn(
                "this match is no longer proposed — it was decided, or its rows moved on;"
                " the suggestion is withdrawn"
            )
        from purser import matching  # function-local: matching imports this module

        if decision == "accept":
            created = matching.confirm_match(
                session, match_id=proposal.id, token=token, via_suggestion=row.id
            )
            row.status = STATUS_ACCEPTED
            row.result_entity_type = "match"
            row.result_entity_id = created.id
        else:
            created = matching.reject_match(
                session, match_id=proposal.id, token=token, via_suggestion=row.id
            )
            row.status = STATUS_REJECTED
            row.reject_scope = "exact"
    elif decision in ("accept", "edit_then_accept"):
        if row.kind == KIND_TRANSFER_LINK:
            created, applied = _accept_transfer_link(
                session, row, decision=decision, edits=edits, token=token
            )
            row.result_entity_type = "rule"
        else:
            created, applied = _accept_schedule(
                session, row, decision=decision, edits=edits, token=token
            )
            row.result_entity_type = "schedule"
        row.status = STATUS_ACCEPTED
        row.result_entity_id = created.id
        row.draft = applied
    elif decision == "reject":
        if reject_scope not in REJECT_SCOPES:
            raise SuggestionError("reject_scope must be exact | counterparty")
        row.status = STATUS_REJECTED
        row.reject_scope = reject_scope
    else:  # snooze
        row.snooze_until = snooze_until or (date_type.today() + timedelta(days=DEFAULT_SNOOZE_DAYS))
        if row.snooze_until <= date_type.today():
            raise SuggestionError("snooze_until must be in the future")

    if decision != "snooze":
        row.decided_at = now
        row.decided_by = token.id
    if note:
        row.decision_note = note
    row.updated_at = now
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id,
        action="suggestion.decide",
        entity_type="suggestion",
        entity_id=row.id,
        before=before,
        after={
            "decision": decision,
            "status": row.status,
            "reject_scope": row.reject_scope,
            "snooze_until": row.snooze_until.isoformat() if row.snooze_until else None,
            "result_entity_id": str(row.result_entity_id) if row.result_entity_id else None,
        },
    )
    events.emit(
        session,
        type_="suggestion.decided",
        entity_type="suggestion",
        entity_id=row.id,
        payload={
            "kind": row.kind,
            "decision": decision,
            "status": row.status,
            "result_entity_type": row.result_entity_type,
            "result_entity_id": str(row.result_entity_id) if row.result_entity_id else None,
        },
    )
    return row, created


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _check_kind(kind: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise SuggestionError(f"kind must be one of {', '.join(KINDS)}")


def _check_status(status: str) -> None:
    if status != "all" and status not in STATUSES:
        raise SuggestionError(f"status must be one of {', '.join((*STATUSES, 'all'))}")


def list_suggestions(
    session: Session,
    *,
    status: str = STATUS_OPEN,
    kind: str | None = None,
    account_ref: str | None = None,
    include_snoozed: bool = False,
    limit: int = LIST_MAX_ROWS,
) -> list[Suggestion]:
    _check_kind(kind)
    _check_status(status)
    stmt = select(Suggestion).order_by(
        Suggestion.confidence.desc().nulls_last(), Suggestion.created_at
    )
    if status != "all":
        stmt = stmt.where(Suggestion.status == status)
    if kind is not None:
        stmt = stmt.where(Suggestion.kind == kind)
    if account_ref is not None:
        account = ledger_service.resolve_account(session, account_ref)
        stmt = stmt.where(
            or_(
                Suggestion.subject_account_id == account.id,
                Suggestion.counterparty_account_id == account.id,
            )
        )
    if status == STATUS_OPEN and not include_snoozed:
        stmt = stmt.where(
            or_(Suggestion.snooze_until.is_(None), Suggestion.snooze_until <= date_type.today())
        )
    return list(session.execute(stmt.limit(min(limit, LIST_MAX_ROWS))).scalars())


def open_count(
    session: Session, *, account_id: uuid.UUID | None = None, kind: str | None = None
) -> int:
    """Open, un-snoozed rows — all kinds (the inbox), or one ``kind``: the
    forecast and the subscription radar count only schedule suggestions,
    since a transfer-link proposal changes no expected transaction."""
    _check_kind(kind)
    stmt = select(func.count(Suggestion.id)).where(
        Suggestion.status == STATUS_OPEN,
        or_(Suggestion.snooze_until.is_(None), Suggestion.snooze_until <= date_type.today()),
    )
    if kind is not None:
        stmt = stmt.where(Suggestion.kind == kind)
    if account_id is not None:
        stmt = stmt.where(
            or_(
                Suggestion.subject_account_id == account_id,
                Suggestion.counterparty_account_id == account_id,
            )
        )
    return int(session.execute(stmt).scalar_one())


def headline(row: Suggestion) -> str:
    """One line an agent (or a card) can show — for a schedule who, cadence
    and how much; for a transfer link the pair and the payee it is scoped
    to; for a match the bill against the bank row and the variance."""
    draft = row.draft
    if row.kind == KIND_MATCH:
        cents = Decimal("0.01")
        expected = Decimal(str(draft.get("expected_amount"))).quantize(cents)
        posted = Decimal(str(draft.get("posted_amount"))).quantize(cents)
        variance = Decimal(str(draft.get("variance"))).quantize(cents)
        return (
            f"Match {draft.get('label')} ({expected} due {draft.get('expected_date')}) to the "
            f"{posted} bank row of {draft.get('posted_date')} on {draft.get('account')}"
            f" — variance {variance}"
        )
    if row.kind == KIND_TRANSFER_LINK:
        return (
            f"Link transfers between {draft.get('from_account')} and {draft.get('to_account')}"
            f" when the payee is {draft.get('payee')} (or an alias)"
        )
    from purser.core import forecasting as core_forecasting

    cadence = core_forecasting.cadence_label(str(draft.get("rrule", "")))
    amount = draft.get("amount")
    how_much = (
        f"~{Decimal(str(amount)).quantize(Decimal('0.01'))}" if amount else "amount from history"
    )
    with_whom = draft.get("counterparty_account") or draft.get("payee") or draft.get("name")
    return f"{draft.get('name')}: {cadence} {how_much} on {draft.get('account')} with {with_whom}"


def serialize(session: Session, row: Suggestion, *, with_evidence: bool = False) -> dict[str, Any]:
    """The compact shape shared by REST and MCP (guardrail 6: evidence is
    summarized unless asked for). Both kinds keep their evidence as
    ``occurrences`` — detected rows for a schedule, confirmed pairs for a
    transfer link — so the summary reads the same way for either."""
    evidence = row.evidence or {}
    occurrences = evidence.get("occurrences") or []
    amounts = [Decimal(str(o["amount"])) for o in occurrences if o.get("amount") is not None]
    summary: dict[str, Any] = {
        "count": len(occurrences),
        "first": occurrences[0]["date"] if occurrences else None,
        "last": occurrences[-1]["date"] if occurrences else None,
        "amount_range": (
            {
                "low": str(min(amounts).quantize(Decimal("0.01"))),
                "high": str(max(amounts).quantize(Decimal("0.01"))),
            }
            if amounts
            else None
        ),
        "cadence": evidence.get("cadence"),
    }
    if evidence.get("note"):
        # An agent's proposal note (#250), kept encrypted with the evidence.
        summary["note"] = evidence["note"]
    out: dict[str, Any] = {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "source": row.source,
        "confidence": str(row.confidence) if row.confidence is not None else None,
        "headline": headline(row),
        "draft": row.draft,
        "evidence_summary": summary,
        "snooze_until": row.snooze_until.isoformat() if row.snooze_until else None,
        "seen_count": row.seen_count,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "reject_scope": row.reject_scope,
        "result_entity_type": row.result_entity_type,
        "result_entity_id": str(row.result_entity_id) if row.result_entity_id else None,
    }
    if with_evidence:
        out["evidence"] = evidence
    return out


def payee_for(session: Session, payee_id: uuid.UUID | None) -> Payee | None:
    return session.get(Payee, payee_id) if payee_id else None
