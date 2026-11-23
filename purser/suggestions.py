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
- ``transfer_pair`` (#386, decision 102) — a :class:`TransferPairDraft` for a
  transfer CANDIDATE: two one-sided bank rows of exactly opposite amounts,
  close in date, that no signal links on their own. The candidate list stays
  the source of truth (it is recomputed from the ledger); the card adds
  snooze and rejection memory. Accept confirms the pair (``confirm_transfer``)
  and reject is remembered as "not a transfer", which the automatic link
  paths honour too. A card whose pair stops being a candidate, or that the
  auto-linker links, is ``withdrawn``.
- ``split`` (#590, decision 118) — a :class:`SplitDraft`: "split like last
  time?" for a new bank row from a payee whose most recent split a person or
  an agent made. Accept applies the copied lines through
  ``splits.split_transaction`` (``edit_then_accept`` with edited ``lines``);
  reject is remembered. A card whose row stopped being a plain bank row (split,
  linked, voided) is ``withdrawn``.

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
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_value_type
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from purser import audit, clock, events
from purser import ledger as ledger_service
from purser import lineage as bank_lineage
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
from purser.privacy import MINUS_SIGN

KIND_SCHEDULE = "schedule"
KIND_TRANSFER_LINK = "transfer_link"
KIND_MATCH = "match"
KIND_TRANSFER_PAIR = "transfer_pair"
KIND_SPLIT = "split"
KIND_TRADE_RULE = "trade_rule"
KINDS = (
    KIND_SCHEDULE,
    KIND_TRANSFER_LINK,
    KIND_MATCH,
    KIND_TRANSFER_PAIR,
    KIND_SPLIT,
    KIND_TRADE_RULE,
)
SOURCE_MARK = "mark"
TRADE_RULE_DETECTOR = "securities_trade_mark"
# The words of a marked row's payee (or description) a trade rule's draft
# starts with: brokers word a buy "YOU BOUGHT <fund>", "Buy <fund>",
# "Reinvestment <fund>", so the first words name the kind of row and the
# rest the security. The person edits the draft before accepting it.
TRADE_RULE_PREFIX_WORDS = 2
SOURCE_CONFIRM = "confirm"
SOURCE_DETECTED = "detected"
STATUS_OPEN = "open"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_WITHDRAWN = "withdrawn"
STATUSES = (STATUS_OPEN, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_WITHDRAWN)
MATCH_DETECTOR = "matcher"
TRANSFER_PAIR_DETECTOR = "transfer_pairing"
# A transfer candidate earns a card only while its newer leg is this recent
# (#386): older pairs stay in the register's whole-history candidate list.
# Without a bound, the first pass over an imported history floods the inbox.
TRANSFER_PAIR_LOOKBACK_DAYS = 60
TRANSFER_PAIR_PREFIX = "transfer_pair:"
SPLIT_PREFIX = "split:"
SPLIT_DETECTOR = "split_like_last_time"
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
    dtstart: date_value_type
    date_type: Literal["fixed", "estimated"] = "fixed"
    amount_type: Literal["fixed", "estimated"] = "fixed"
    amount: Decimal | None = None
    estimate_basis: (
        Literal["last", "rolling_avg_3", "rolling_avg_12", "counterparty_balance"] | None
    ) = None
    payee: str | None = None
    # #534: drafts stored before it carry none; the label rules (name split)
    # then apply when the draft is accepted.
    memo: str | None = Field(default=None, max_length=200)
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
    effective_from: date_value_type | None = None

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


class TradeRuleDraft(BaseModel):
    """The typed draft a trade_rule suggestion carries (#591, decision 119):
    exactly the categorize rule accepting it creates, as a DRAFT, that marks
    rows like one a person marked as a securities trade. Scoped to the
    marked row's account (a feed-holdings brokerage account, the only kind a
    securities-trade rule may name) and drafted from its payee or
    description; the text predicate and the direction are editable, the
    account is the suggestion's identity."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    account: str
    account_id: uuid.UUID
    match_field: Literal["payee", "description", "payee_or_description"] = "payee_or_description"
    match_mode: Literal["equals", "prefix", "contains"] = "prefix"
    match_text: str = Field(min_length=1, max_length=200)
    direction: Literal["debit", "credit"] | None = None

    @field_validator("match_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("match_text must not be blank")
        return value.strip()

    def stored(self) -> dict[str, Any]:
        data = self.model_dump()
        data["account_id"] = str(self.account_id)
        return data


TRADE_RULE_EDITABLE = frozenset({"match_field", "match_mode", "match_text", "direction"})


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
    expected_date: date_value_type
    expected_amount: Decimal
    posted_date: date_value_type
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


class TransferPairDraft(BaseModel):
    """The typed draft a transfer_pair card carries (#386, decision 102): the
    candidate as the card shows it — the two bank rows, the accounts the
    money left and arrived in, the amount moved (positive), both dates and
    both bank texts. Money as a decimal string when stored.

    Nothing is editable: the pair IS the suggestion — linking other rows is
    ``confirm_transfer``, not an edit. Versioned like :class:`ScheduleDraft`."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    outgoing_id: uuid.UUID
    incoming_id: uuid.UUID
    from_account: str
    to_account: str
    amount: Decimal
    outgoing_date: date_value_type
    incoming_date: date_value_type
    date_gap_days: int
    outgoing_description: str | None = None
    incoming_description: str | None = None

    def stored(self) -> dict[str, Any]:
        """JSON-safe form for the encrypted column (money as strings)."""
        return {
            "version": self.version,
            "outgoing_id": str(self.outgoing_id),
            "incoming_id": str(self.incoming_id),
            "from_account": self.from_account,
            "to_account": self.to_account,
            "amount": str(self.amount),
            "outgoing_date": self.outgoing_date.isoformat(),
            "incoming_date": self.incoming_date.isoformat(),
            "date_gap_days": self.date_gap_days,
            "outgoing_description": self.outgoing_description,
            "incoming_description": self.incoming_description,
        }


class SplitDraftLine(BaseModel):
    """One copied line of a :class:`SplitDraft`: the target (a category path
    or ``[account path]``), its amount in the flat form's sign, its memo, and
    for an account line the one exact backing row found, if any."""

    model_config = ConfigDict(extra="forbid")

    target: str
    amount: Decimal
    memo: str | None = None
    backing_ids: list[uuid.UUID] = Field(default_factory=list)


class SplitDraft(BaseModel):
    """The typed draft a split card carries (#590, decision 118): the new
    bank row (``transaction_id``, its account, date and amount), the split it
    copies (``template_id``) and the lines to apply — the template's lines,
    with the amount difference on its largest category line. Only ``lines``
    is editable (the row is the suggestion's identity)."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    transaction_id: uuid.UUID
    template_id: uuid.UUID
    account: str
    date: date_value_type
    amount: Decimal
    template_amount: Decimal
    payee: str | None = None
    lines: list[SplitDraftLine]

    def stored(self) -> dict[str, Any]:
        """JSON-safe form for the encrypted column (money as strings)."""
        return {
            "version": self.version,
            "transaction_id": str(self.transaction_id),
            "template_id": str(self.template_id),
            "account": self.account,
            "date": self.date.isoformat(),
            "amount": str(self.amount),
            "template_amount": str(self.template_amount),
            "payee": self.payee,
            "lines": [
                {
                    "target": line.target,
                    "amount": str(line.amount),
                    "memo": line.memo,
                    "backing_ids": [str(i) for i in line.backing_ids],
                }
                for line in self.lines
            ],
        }


SPLIT_EDITABLE = frozenset({"lines"})


def split_fingerprint(root_id: uuid.UUID) -> str:
    """A split card names its bank row by the first row of the row's
    correction chain, in plain text: the categorize pass correcting the row
    keeps the card, and a rejection outlives it."""
    return f"{SPLIT_PREFIX}{root_id}"


@dataclass(frozen=True)
class TransferPairConfirmed:
    """What accepting a transfer_pair card produced: the merged transfer,
    and the transfer_link suggestion the confirm proposed for the pair and
    its payee (decision 67), when it proposed one."""

    merged: Transaction
    proposed: Suggestion | None


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
    """A new open row, its audit row and its ``suggestion.created`` event —
    or, when another session created the fingerprint first, that row
    (``_create_once``)."""
    row, _ = _create_once(
        session,
        kind=kind,
        draft=draft,
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
        event_payload=event_payload,
    )
    return row


def _create_once(
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
) -> tuple[Suggestion, bool]:
    """``(row, True)`` for a new open row, with its audit row and its
    ``suggestion.created`` event; ``(existing, False)`` when another session
    inserted the fingerprint first (#386 review). Two passes — an import and
    the matching job — can both look, find nothing, and insert: the insert
    is ``INSERT … ON CONFLICT (fingerprint) DO NOTHING``, which waits for the
    other session's insert and then does nothing, so the caller's import or
    job goes on and the winner's row is read back instead. No SAVEPOINT: its
    release would run the session's commit hook (a balance recompute) at
    each new row, and past 64 of them overflow the backend's subtransaction
    cache."""
    now = datetime.now(UTC)
    new_id = session.execute(
        pg_insert(Suggestion)
        .values(
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
        .on_conflict_do_nothing(index_elements=[Suggestion.fingerprint])
        .returning(Suggestion.id)
    ).scalar_one_or_none()
    if new_id is None:
        existing = session.execute(
            select(Suggestion).where(Suggestion.fingerprint == fingerprint)
        ).scalar_one()
        return existing, False
    row = session.get(Suggestion, new_id)
    assert row is not None  # inserted just now, in this transaction
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
    return row, True


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
    want to skip the work entirely. A row another session created first
    (the nightly detector and an agent's proposal racing) is refreshed like
    any open row, so this caller's draft and evidence — an agent's note —
    are stored, not dropped (#386 review)."""
    row = session.execute(
        select(Suggestion).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is None:
        row, created = _create_once(
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
        if created:
            return row, True
    if row.status == STATUS_WITHDRAWN:
        session.refresh(row, with_for_update=True)
    if row.status == STATUS_WITHDRAWN and row.detector == "recurrence_v2":
        row.status = STATUS_OPEN
        row.decided_at = None
        row.decided_by = None
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="suggestion.reopen",
            entity_type="suggestion",
            entity_id=row.id,
            before={"status": STATUS_WITHDRAWN},
            after={"status": STATUS_OPEN},
        )
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
        _add_confirmed_pair(session, open_row, payee, payee_root, occurrence, token)
        return open_row

    draft_model = TransferLinkDraft(
        from_account=from_account.path,
        to_account=to_account.path,
        payee=payee.name,
        payee_id=payee_root,
    )
    row, created = _create_once(
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
    if not created:
        # Another confirm created the suggestion first: this pair is one
        # more piece of its evidence, as when the row already existed (#386
        # review) — not dropped behind the winner's row.
        if row.status != STATUS_OPEN:
            return None
        _add_confirmed_pair(session, row, payee, payee_root, occurrence, token)
    return row


def _add_confirmed_pair(
    session: Session,
    row: Suggestion,
    payee: Payee,
    payee_root: uuid.UUID,
    occurrence: dict[str, str],
    token: ApiToken,
) -> None:
    """One more confirmed pair on an open transfer_link suggestion: its
    occurrence joins the evidence (once per merged transaction), and the
    row counts one more sighting."""
    occurrences = [
        o
        for o in (row.evidence or {}).get("occurrences", [])
        if o.get("transaction_id") != occurrence["transaction_id"]
    ]
    occurrences.append(occurrence)
    # Date order, as detection evidence is: an older pair confirmed later
    # still reads as the first, and the cap keeps the most recent pairs.
    occurrences.sort(key=lambda o: (o["date"], o["transaction_id"]))
    # The pair and the user's direction stay as first seen; the payee
    # follows its aliases, so the card names the payee as it is today.
    draft = {**row.draft, "payee": payee.name, "payee_id": str(payee_root)}
    _refresh(
        session,
        row,
        draft=TransferLinkDraft.model_validate(draft).stored(),
        evidence={"occurrences": occurrences[-EVIDENCE_OCCURRENCES_CAP:]},
        confidence=None,
        detector=CONFIRM_DETECTOR,
        detector_version=1,
        actor_token_id=token.id,
    )


# --------------------------------------------------------------------------
# trade_rule — proposed by marking a row a securities trade (#591, decision 119)
# --------------------------------------------------------------------------


def trade_rule_fingerprint(account_id: uuid.UUID, match_text: str, direction: str | None) -> str:
    """Stable identity of a trade_rule suggestion: the account, the text
    (by its blind index, so no narrative reaches the plaintext column) and
    the direction."""
    text_key = rules_service.blind_index_for(match_text)
    return hashlib.sha256(
        f"trade_rule:{account_id}:{text_key}:{direction or 'any'}".encode()
    ).hexdigest()


def draft_trade_rule(tx: Transaction) -> TradeRuleDraft | None:
    """The rule a mark suggests: the row's account, the first words of its
    payee (else its description) as a prefix over payee or description, and
    its direction. None for a row with no text to match on."""
    real = _real_leg(tx)
    if real is None:
        return None
    text = (tx.payee.name if tx.payee else None) or tx.description
    words = (text or "").split()
    if not words:
        return None
    return TradeRuleDraft(
        account=real.account.path,
        account_id=real.account_id,
        match_text=" ".join(words[:TRADE_RULE_PREFIX_WORDS])[:200],
        direction="debit" if Decimal(real.amount) < 0 else "credit",
    )


def _trade_rule_covers(session: Session, tx: Transaction, account_id: uuid.UUID) -> bool:
    """A securities-trade rule on this account (draft, active or disabled;
    archived is deleted) already matches the marked row."""
    from purser.core import rules as core_rules

    securities = ledger_service.securities_account(session)
    real = _real_leg(tx)
    assert real is not None
    candidate = core_rules.Candidate(
        account_id=str(account_id),
        amount=Decimal(real.amount),
        date=tx.date,
        payee=tx.payee.name if tx.payee else None,
        description=tx.description,
    )
    rows = session.execute(
        select(Rule).where(
            Rule.type == rules_service.TYPE_CATEGORIZE,
            Rule.state != rules_service.STATE_ARCHIVED,
            Rule.category_id == securities.id,
        )
    ).scalars()
    return any(
        row.account_ids
        and account_id in row.account_ids
        and core_rules.matches(rules_service._predicate_of(row), candidate)
        for row in rows
    )


def propose_trade_rule(session: Session, *, tx: Transaction, token: ApiToken) -> Suggestion | None:
    """After a person or an agent marked ``tx`` a securities trade: upsert
    the trade_rule suggestion for a rule that marks rows like it, or return
    None when there is nothing to propose (decision 67's shape). It writes
    no rule: accepting creates one as a DRAFT, which marks nothing until it
    is activated; its projected count comes back with the accept.

    Nothing is proposed when the row has no payee or description, a
    securities-trade rule on the account already matches it, the account's
    trade rules were rejected with ``reject_scope='counterparty'``, or this
    draft was rejected or accepted before. A repeat mark of a row the same
    draft covers refreshes the open card (``seen_count``, its evidence)."""
    draft = draft_trade_rule(tx)
    if draft is None:
        return None
    account_id = draft.account_id
    if _trade_rule_covers(session, tx, account_id):
        return None
    rows = list(
        session.execute(
            select(Suggestion).where(
                Suggestion.kind == KIND_TRADE_RULE, Suggestion.subject_account_id == account_id
            )
        ).scalars()
    )
    if any(r.status == STATUS_REJECTED and r.reject_scope == "counterparty" for r in rows):
        return None
    fingerprint = trade_rule_fingerprint(account_id, draft.match_text, draft.direction)
    same = next((r for r in rows if r.fingerprint == fingerprint), None)
    real = _real_leg(tx)
    assert real is not None
    occurrence = {
        "date": tx.date.isoformat(),
        "amount": str(abs(Decimal(real.amount))),
        "transaction_id": str(tx.id),
    }
    if same is not None:
        if same.status != STATUS_OPEN:
            return None  # rejected (remembered) or accepted (its rule covers it)
        _add_marked_row(session, same, occurrence, token)
        return same
    row, created = _create_once(
        session,
        kind=KIND_TRADE_RULE,
        draft=draft.stored(),
        source=SOURCE_MARK,
        fingerprint=fingerprint,
        subject_account_id=account_id,
        subject_payee_id=tx.payee_id,
        counterparty_account_id=None,
        evidence={"occurrences": [occurrence]},
        confidence=None,
        detector=TRADE_RULE_DETECTOR,
        detector_version=1,
        actor_token_id=token.id,
        event_payload={"account": draft.account},
    )
    if not created:
        if row.status != STATUS_OPEN:
            return None
        _add_marked_row(session, row, occurrence, token)
    return row


def _add_marked_row(
    session: Session, row: Suggestion, occurrence: dict[str, Any], token: ApiToken
) -> None:
    """One more marked row on an open trade_rule card: its evidence, capped."""
    evidence = dict(row.evidence or {})
    seen = [o for o in evidence.get("occurrences") or [] if o.get("transaction_id")]
    if all(o["transaction_id"] != occurrence["transaction_id"] for o in seen):
        seen.append(occurrence)
    evidence["occurrences"] = seen[-EVIDENCE_OCCURRENCES_CAP:]
    _refresh(
        session,
        row,
        draft=row.draft,
        evidence=evidence,
        confidence=None,
        detector=TRADE_RULE_DETECTOR,
        detector_version=1,
        actor_token_id=token.id,
    )


def draft_from_create_args(
    session: Session,
    *,
    name: str,
    account_ref: str,
    rrule: str,
    dtstart: date_value_type | None,
    amount_type: str,
    amount: Decimal | None,
    estimate_basis: str | None,
    payee_name: str | None,
    category_path: str | None,
    autopost_policy: str,
    counterparty_account_ref: str | None,
    date_mode: str = "fixed",
    memo: str | None = None,
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
            dtstart=dtstart or clock.ledger_today(),
            date_type=date_mode,
            amount_type=amount_type,
            amount=amount,
            estimate_basis=estimate_basis,
            payee=payee_name,
            memo=memo,
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
    return not (held and posted.date >= clock.ledger_today() - window)


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
    # A proposal's persisted variance can predate an edited expectation.
    # The card must describe the two amounts it is actually showing.
    variance = posted_amount - expected_amount
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
    _reopen(
        session,
        row,
        draft=stored,
        evidence=evidence,
        confidence=confidence,
        subject_account_id=subject_account_id,
        subject_payee_id=expected.payee_id,
        counterparty_account_id=counterparty_account_id,
        actor_token_id=actor_token_id,
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
    _close_card(
        session,
        row,
        status=status,
        actor_token_id=actor_token_id,
        reason=reason,
        result=("match", match.id),
        withdraw_context={"match_id": str(match.id)},
    )


def _still_open(session: Session, row: Suggestion) -> bool:
    """Lock ``row`` and re-read it: whether it is still open (#497 review).
    A sync or merge picks the cards it closes from a plain read; a person
    may have rejected one since, and a system close must not overwrite that."""
    session.refresh(row, with_for_update=True)
    return row.status == STATUS_OPEN


def _close_card(
    session: Session,
    row: Suggestion,
    *,
    status: str,
    actor_token_id: uuid.UUID | None,
    reason: str,
    result: tuple[str, uuid.UUID] | None = None,
    withdraw_context: dict[str, str] | None = None,
) -> bool:
    """Close a mirror card (match, transfer_pair) because what it mirrors
    was decided outside the card or went away: ``accepted`` records
    ``result`` (entity type, id) as what accepting produced, ``rejected``
    rejects exactly, ``withdrawn`` is a system action and audits
    ``suggestion.withdraw`` with ``withdraw_context``.

    A system close (``withdrawn``, or ``accepted`` because what the card
    mirrors was decided elsewhere) applies only to a card still open under
    its row lock (``_still_open``): a person's decision committed meanwhile
    stands. Returns whether the card was closed."""
    if status in (STATUS_WITHDRAWN, STATUS_ACCEPTED) and not _still_open(session, row):
        return False
    before = {"status": row.status}
    now = datetime.now(UTC)
    row.status = status
    row.decided_at = now
    row.decided_by = actor_token_id if status != STATUS_WITHDRAWN else None
    if status == STATUS_REJECTED:
        row.reject_scope = "exact"
    if status == STATUS_ACCEPTED and result is not None:
        row.result_entity_type, row.result_entity_id = result
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
            after={"fingerprint": row.fingerprint, **(withdraw_context or {}), "reason": reason},
        )
        return True
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
    return True


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
        if _withdraw_orphan(session, row, actor_token_id=actor_token_id):
            stats["withdrawn"] += 1
    return stats


def _withdraw_orphan(
    session: Session, row: Suggestion, *, actor_token_id: uuid.UUID | None, reason: str = "sweep"
) -> bool:
    """Withdraw a mirror card whose subject is gone — a match card whose
    proposal is gone, a transfer_pair card whose pair is no longer a
    candidate — from a sweep or a stale decide. Only a card still open
    under its lock (``_still_open``); returns whether it was withdrawn."""
    if not _still_open(session, row):
        return False
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
    return True


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


def _reopen(
    session: Session,
    row: Suggestion,
    *,
    draft: dict[str, Any],
    evidence: dict[str, Any] | None,
    confidence: Decimal | None,
    subject_account_id: uuid.UUID,
    subject_payee_id: uuid.UUID | None,
    counterparty_account_id: uuid.UUID | None,
    actor_token_id: uuid.UUID | None,
    keep_snooze: bool = False,
) -> None:
    """A withdrawn or accepted mirror card whose subject is live again (a
    match proposed again, a pair a candidate again) opens once more: its
    decision fields are cleared, and it counts one more sighting.
    ``keep_snooze`` keeps a snooze the card carried when it was withdrawn
    (a transfer_pair card: "ask me later" outlives the withdrawal, #386
    review); a match card re-opens unsnoozed, as since decision 86."""
    reopened_from = row.status
    now = datetime.now(UTC)
    row.status = STATUS_OPEN
    row.draft = draft
    row.evidence = evidence
    row.confidence = confidence
    row.subject_account_id = subject_account_id
    row.subject_payee_id = subject_payee_id
    row.counterparty_account_id = counterparty_account_id
    if not keep_snooze:
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
            "fingerprint": row.fingerprint,
            "seen_count": row.seen_count,
            "reopened_from": reopened_from,
        },
    )


# --------------------------------------------------------------------------
# transfer_pair — a transfer candidate as a card (#386, decision 102)
# --------------------------------------------------------------------------


def transfer_pair_fingerprint(pair_key: str) -> str:
    """Stable identity of a transfer_pair card, in plain text: the card key
    of its candidate (``<low id>:<high id>``, ``matching.card_keys`` — the
    first rows of the two correction chains, so a correction never gives a
    pair a new card; for rows never corrected it is the plain pair key, the
    ``dedup_key`` that ``transfer.candidate`` has always carried). Unordered,
    like the pair."""
    return f"{TRANSFER_PAIR_PREFIX}{pair_key}"


def _pair_ids(fingerprint: str) -> tuple[uuid.UUID, uuid.UUID]:
    low, high = fingerprint[len(TRANSFER_PAIR_PREFIX) :].split(":")
    return uuid.UUID(low), uuid.UUID(high)


def rejected_transfer_pair_rows(
    session: Session,
) -> dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID]:
    """The rows of each rejected transfer_pair suggestion -> its id: the
    "not a transfer" memory (#386) that ``transfers._suppressed_pairs`` hands
    every AUTO link path, the candidate list and the inbox. The pair is
    remembered by bank line (``matching.lineage_pairs``): a row the bank
    posted since (a pending's posted successor) or a correction carried on
    (its replacement) is the same bank line, so the pair it forms with the
    other side stays rejected (#386 review). Without that, rejecting a
    pending pair would last only until its rows posted, and a rejected pair
    only until the categorize pass corrected a row — when a name signal
    could link it at once."""
    return {
        _pair_ids(fingerprint): row_id
        for row_id, fingerprint in session.execute(
            select(Suggestion.id, Suggestion.fingerprint).where(
                Suggestion.kind == KIND_TRANSFER_PAIR, Suggestion.status == STATUS_REJECTED
            )
        ).all()
    }


def open_transfer_pair_cards(session: Session) -> dict[str, uuid.UUID]:
    """Fingerprint -> id of every OPEN transfer_pair card, snoozed ones
    included: what the candidate list names as each pair's
    ``suggestion_id``."""
    return {
        fingerprint: row_id
        for row_id, fingerprint in session.execute(
            select(Suggestion.id, Suggestion.fingerprint).where(
                Suggestion.kind == KIND_TRANSFER_PAIR, Suggestion.status == STATUS_OPEN
            )
        ).all()
    }


def _transfer_pair_card(
    outgoing: Transaction, incoming: Transaction, *, amount: Decimal, gap_days: int
) -> tuple[TransferPairDraft, dict[str, Any], uuid.UUID, uuid.UUID] | None:
    """(draft, evidence, subject account, counterparty account): the money
    left the subject account and arrived in the counterparty one. The
    evidence is the pair itself, in the shape a transfer_link's confirmed
    pairs take (left, arrived, amount)."""
    out_leg, in_leg = _real_leg(outgoing), _real_leg(incoming)
    if out_leg is None or in_leg is None:
        return None
    moved = abs(Decimal(amount))
    draft = TransferPairDraft(
        outgoing_id=outgoing.id,
        incoming_id=incoming.id,
        from_account=out_leg.account.path,
        to_account=in_leg.account.path,
        amount=moved,
        outgoing_date=outgoing.date,
        incoming_date=incoming.date,
        date_gap_days=gap_days,
        outgoing_description=outgoing.description,
        incoming_description=incoming.description,
    )
    evidence = {
        "occurrences": [
            {
                "date": outgoing.date.isoformat(),
                "incoming_date": incoming.date.isoformat(),
                "amount": str(moved),
                "outgoing_id": str(outgoing.id),
                "incoming_id": str(incoming.id),
            }
        ]
    }
    return draft, evidence, out_leg.account_id, in_leg.account_id


def _create_transfer_pair(
    session: Session,
    *,
    fingerprint: str,
    draft: TransferPairDraft,
    evidence: dict[str, Any],
    subject_account_id: uuid.UUID,
    counterparty_account_id: uuid.UUID,
    actor_token_id: uuid.UUID | None,
) -> tuple[Suggestion, bool]:
    """``_create_once`` for a transfer_pair card: ``(row, created)``."""
    return _create_once(
        session,
        kind=KIND_TRANSFER_PAIR,
        draft=draft.stored(),
        source=SOURCE_DETECTED,
        fingerprint=fingerprint,
        subject_account_id=subject_account_id,
        subject_payee_id=None,
        counterparty_account_id=counterparty_account_id,
        evidence=evidence,
        confidence=None,  # nothing scores a candidate: amount and date are all it has
        detector=TRANSFER_PAIR_DETECTOR,
        detector_version=1,
        actor_token_id=actor_token_id,
        event_payload={
            "account": draft.from_account,
            "counterparty_account": draft.to_account,
            "amount": str(draft.amount),
        },
    )


def upsert_transfer_pair_suggestion(
    session: Session,
    *,
    outgoing: Transaction,
    incoming: Transaction,
    amount: Decimal,
    gap_days: int,
    actor_token_id: uuid.UUID | None = None,
) -> tuple[Suggestion | None, bool]:
    """The card for a transfer candidate (``transfers.sync_candidate_suggestions``):
    created, or re-opened when a withdrawn card's pair is a candidate again —
    ``(row, True)`` — or quietly refreshed when what it shows changed —
    ``(row, False)``. ``(None, False)`` when the pair was rejected: the
    memory stands (the sync should not have offered it)."""
    from purser import matching  # function-local: matching imports this module

    parts = _transfer_pair_card(outgoing, incoming, amount=amount, gap_days=gap_days)
    if parts is None:
        return None, False
    draft, evidence, subject_account_id, counterparty_account_id = parts
    fingerprint = transfer_pair_fingerprint(
        matching.card_keys(session, [(outgoing.id, incoming.id)])[(outgoing.id, incoming.id)]
    )
    stored = draft.stored()
    row = session.execute(
        select(Suggestion).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is None:
        row, created = _create_transfer_pair(
            session,
            fingerprint=fingerprint,
            draft=draft,
            evidence=evidence,
            subject_account_id=subject_account_id,
            counterparty_account_id=counterparty_account_id,
            actor_token_id=actor_token_id,
        )
        if created:
            return row, True
        # Another session created it first: go on as with any existing row.
    if row.status == STATUS_REJECTED:
        return None, False
    if row.status == STATUS_OPEN:
        if row.draft != stored:
            # A moved account's path, an edited pending description: the card
            # shows the pair as it is now.
            row.draft = stored
            row.evidence = evidence
            row.subject_account_id = subject_account_id
            row.counterparty_account_id = counterparty_account_id
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
        return row, False
    _reopen(
        session,
        row,
        draft=stored,
        evidence=evidence,
        confidence=None,
        subject_account_id=subject_account_id,
        subject_payee_id=None,
        counterparty_account_id=counterparty_account_id,
        actor_token_id=actor_token_id,
        keep_snooze=True,
    )
    return row, True


def withdraw_transfer_pair(
    session: Session, row: Suggestion, *, actor_token_id: uuid.UUID | None, reason: str
) -> bool:
    """The sync's withdrawal of an open card whose pair stopped being a
    candidate (a row merged, absorbed, matched to a bill or voided; the pair
    unlinked; a closer row paired instead). False when it was no longer
    open: a person decided it meanwhile."""
    return _withdraw_orphan(session, row, actor_token_id=actor_token_id, reason=reason)


def on_transfer_pair_merged(
    session: Session,
    *,
    outgoing_id: uuid.UUID,
    incoming_id: uuid.UUID,
    merged_id: uuid.UUID,
    token_id: uuid.UUID | None,
    accepted: bool,
    reason: str,
    via_suggestion: uuid.UUID | None = None,
) -> None:
    """``matching._merge_transfer_pair`` merged these two rows (#386). The
    open card for exactly this pair becomes ``accepted`` — the merged
    transfer is what it produced — only when ``accepted``: a person's (or
    agent's) explicit confirm of the pair, ``confirm_transfer``. Any other
    merge withdraws it, whoever ran it: the auto-linker, a conversion on the
    machine path (the categorize pass, a rule's apply-history) even under a
    token, an '[account]' edit that picked the counterpart itself — nobody
    decided the pair the card showed (#386 review). Any other open card
    naming either row is withdrawn: that row is gone. ``via_suggestion`` is
    the card being decided right now, which ``decide`` closes itself."""
    from purser import matching  # function-local: matching imports this module

    # Cards name the first row of each correction chain (``matching.card_keys``).
    roots = bank_lineage.correction_roots(session, (outgoing_id, incoming_id))
    fingerprint = transfer_pair_fingerprint(
        matching._pair_key(roots[outgoing_id], roots[incoming_id])
    )
    rows = session.execute(
        select(Suggestion)
        .where(
            Suggestion.kind == KIND_TRANSFER_PAIR,
            Suggestion.status == STATUS_OPEN,
            or_(
                Suggestion.fingerprint.contains(str(roots[outgoing_id]), autoescape=True),
                Suggestion.fingerprint.contains(str(roots[incoming_id]), autoescape=True),
            ),
        )
        .order_by(Suggestion.created_at, Suggestion.id)
    ).scalars()
    for row in list(rows):
        if row.id == via_suggestion:
            continue
        _close_card(
            session,
            row,
            status=STATUS_ACCEPTED
            if accepted and row.fingerprint == fingerprint
            else STATUS_WITHDRAWN,
            actor_token_id=token_id,
            reason=reason,
            result=("transaction", merged_id),
            withdraw_context={"merged_id": str(merged_id)},
        )


def reject_transfer_pair(
    session: Session,
    *,
    outgoing_id: uuid.UUID,
    incoming_id: uuid.UUID,
    token: ApiToken,
    note: str | None = None,
) -> tuple[Suggestion, bool]:
    """ "Not a transfer" for any candidate pair, with a card or without one
    and whatever its age (#386): the pair's transfer_pair row becomes
    ``rejected`` — written rejected when the pair has no card (its newer leg
    is older than the card window, or no pass has run since it appeared),
    the open or withdrawn card rejected otherwise — and from then on every
    AUTO link path, the candidate list and the inbox leave the pair alone.
    An explicit ``confirm_transfer`` can still link it, as after an unlink.

    Returns ``(row, changed)``; a pair already rejected comes back unchanged.
    Write scope, like rejecting the card. The ids may name rows that were
    corrected or posted since: each is followed to the row that carries its
    bank line now (#386 review), and those rows must still pair: both live
    in the transfer pool, exactly opposite amounts on two accounts, inside
    the transfer window."""
    if not security.can_write(token.scopes):
        raise SuggestionError("rejecting a transfer candidate requires write scope")
    from purser import matching  # function-local: matching imports this module

    heads = bank_lineage.current_rows(session, (outgoing_id, incoming_id))
    outgoing_id, incoming_id = heads[outgoing_id], heads[incoming_id]
    rejected = matching.lineage_pairs(session, rejected_transfer_pair_rows(session))
    already = rejected.get(outgoing_id, incoming_id)
    if already is not None:
        return get_suggestion(session, already), False
    live = matching.live_transfer_pair(session, outgoing_id, incoming_id)
    if live is None:
        raise SuggestionError(
            "these rows are not a transfer candidate: both must be live one-sided bank rows "
            "of exactly opposite amounts on two accounts, at most "
            f"{core_matching.TRANSFER_WINDOW_DAYS} days apart"
        )
    pair, outgoing, incoming = live
    fingerprint = transfer_pair_fingerprint(
        matching.card_keys(session, [(outgoing.id, incoming.id)])[(outgoing.id, incoming.id)]
    )
    row = session.execute(
        select(Suggestion).where(Suggestion.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is None:
        parts = _transfer_pair_card(
            outgoing, incoming, amount=pair.amount, gap_days=pair.date_gap_days
        )
        assert parts is not None  # live_transfer_pair returned one-sided rows
        draft, evidence, subject_account_id, counterparty_account_id = parts
        row, _ = _create_transfer_pair(
            session,
            fingerprint=fingerprint,
            draft=draft,
            evidence=evidence,
            subject_account_id=subject_account_id,
            counterparty_account_id=counterparty_account_id,
            actor_token_id=token.id,
        )
    if row.status == STATUS_REJECTED:
        return row, False  # rejected meanwhile by another session
    if note:
        row.decision_note = note
    _close_card(
        session,
        row,
        status=STATUS_REJECTED,
        actor_token_id=token.id,
        reason="transfer_candidates.reject",
    )
    return row, True


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
        date_mode=draft.date_type,
        amount=draft.amount,
        token=token,
        payee_name=draft.payee,
        memo=draft.memo,
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


def _accept_trade_rule(
    session: Session,
    row: Suggestion,
    *,
    decision: str,
    edits: dict[str, Any] | None,
    token: ApiToken,
) -> tuple[Rule, dict[str, Any]]:
    """The categorize rule the draft describes, targeting "Securities trade"
    (``equity.securities``), created as a DRAFT: it marks nothing until it
    is activated, and history only through apply-history's dry run first
    (decision 46), exactly like any categorize rule."""
    stored = _edited_draft(row, decision, edits, TRADE_RULE_EDITABLE)
    try:
        draft = TradeRuleDraft.model_validate(stored)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(part) for part in error["loc"])
        raise SuggestionError(f"invalid trade_rule draft: {where}: {error['msg']}") from exc
    fields: dict[str, object] = {
        "type": rules_service.TYPE_CATEGORIZE,
        "account_ids": [draft.account_id],
        "match_field": draft.match_field,
        "match_mode": draft.match_mode,
        "match_text": draft.match_text,
        "category_id": ledger_service.securities_account(session).id,
        "note": f"Accepted from trade-rule suggestion {row.id}",
    }
    if draft.direction is not None:
        fields["direction"] = draft.direction
    try:
        created = rules_service.create(session, fields=fields, token=token, activate=False)
    except rules_service.RuleError as exc:
        raise SuggestionError(f"invalid trade_rule draft: {exc}") from exc
    return created, draft.stored()


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
            else f" (a trade_rule suggestion edits only {', '.join(sorted(editable))};"
            " its account is what it proposes)"
            if row.kind == KIND_TRADE_RULE
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
    if not detector_evidence_current(row):
        raise SuggestionError(
            "This recurring pattern is stale. Run schedule detection again before creating it."
        )
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
    snooze_until: date_value_type | None = None,
) -> tuple[Suggestion, Schedule | Rule | Match | TransferPairConfirmed | None]:
    """accept | edit_then_accept -> what the draft produces: an active
    schedule (``schedule``), a DRAFT transfer_link rule awaiting its
    activation confirm (``transfer_link``), the confirmed match (``match``),
    or the merged transfer plus the transfer_link suggestion its confirm
    proposed (``transfer_pair``); reject -> durable, scoped (a match card
    rejects exactly, and rejects its proposal; a transfer_pair card rejects
    exactly, and the auto paths remember it); snooze -> back in the inbox on
    the date (a match card's proposal stays live and keeps holding its bank
    row). Write scope for every decision: each one changes what Purser
    expects or links."""
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
    created: Schedule | Rule | Match | TransferPairConfirmed | None = None

    if row.kind == KIND_SPLIT and decision in ("accept", "edit_then_accept"):
        from purser import splits  # function-local: splits imports this module

        draft = SplitDraft.model_validate(row.draft)
        if decision == "edit_then_accept":
            unknown = set(edits or {}) - SPLIT_EDITABLE
            if unknown or not edits:
                raise SuggestionError("a split suggestion takes edits to lines only")
            try:
                draft = SplitDraft.model_validate({**draft.stored(), "lines": edits["lines"]})
            except ValidationError as exc:
                raise SuggestionError(f"invalid lines: {exc.errors()[0]['msg']}") from exc
        head = bank_lineage.current_rows(session, [draft.transaction_id])[draft.transaction_id]
        result = splits.split_transaction(
            session,
            tx_id=head,
            lines=[
                splits.LineInput(
                    target=line.target,
                    amount=line.amount,
                    memo=line.memo,
                    backing_ids=tuple(line.backing_ids),
                )
                for line in draft.lines
            ],
            token=token,
            via_suggestion=row.id,
        )
        row.status = STATUS_ACCEPTED
        row.result_entity_type = "transaction"
        row.result_entity_id = uuid.UUID(result["transaction_id"])
        row.draft = draft.stored()
    elif row.kind == KIND_TRANSFER_PAIR and decision != "snooze":
        if decision == "edit_then_accept":
            raise SuggestionError(
                "a transfer_pair suggestion has nothing to edit — use confirm_transfer to link"
                " other rows"
            )
        if decision == "reject" and reject_scope != "exact":
            raise SuggestionError(
                "a transfer_pair suggestion is rejected exactly (reject_scope=exact): it names"
                " one pair"
            )
        from purser import matching  # function-local: matching imports this module

        # The card names the rows of its last refresh; a correction since
        # (the categorize pass) moved a bank line onto a replacement, which
        # is what a decision acts on (#386 review).
        pair = TransferPairDraft.model_validate(row.draft)
        heads = bank_lineage.current_rows(session, (pair.outgoing_id, pair.incoming_id))
        live = matching.live_transfer_pair(
            session, heads[pair.outgoing_id], heads[pair.incoming_id]
        )
        if live is None:
            _withdraw_orphan(session, row, actor_token_id=token.id, reason="no_longer_candidate")
            raise SuggestionWithdrawn(
                "this pair is no longer a transfer candidate — a row was linked, matched,"
                " corrected or voided; the suggestion is withdrawn"
            )
        if decision == "accept":
            merged, proposed = matching.confirm_transfer_and_propose(
                session,
                outgoing_id=live[1].id,
                incoming_id=live[2].id,
                token=token,
                via_suggestion=row.id,
            )
            created = TransferPairConfirmed(merged=merged, proposed=proposed)
            row.status = STATUS_ACCEPTED
            row.result_entity_type = "transaction"
            row.result_entity_id = merged.id
        else:
            row.status = STATUS_REJECTED
            row.reject_scope = "exact"
    elif row.kind == KIND_MATCH and decision != "snooze":
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
        elif row.kind == KIND_TRADE_RULE:
            created, applied = _accept_trade_rule(
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
        row.snooze_until = snooze_until or (
            clock.ledger_today() + timedelta(days=DEFAULT_SNOOZE_DAYS)
        )
        if row.snooze_until <= clock.ledger_today():
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


def _live_match_fingerprints(session: Session) -> set[str]:
    """Read the source of truth, including before a scheduled cleanup runs.

    Fingerprints are plaintext identities. One join checks all proposals;
    counting and paging use the same filter without decrypting every card.
    """
    from sqlalchemy.orm import aliased

    from purser import matching

    posted = aliased(Transaction)
    pairs = session.execute(
        select(Transaction, Match.posted_id)
        .join(Match, Match.expected_id == Transaction.id)
        .join(posted, posted.id == Match.posted_id)
        .where(
            Match.state == "proposed",
            Transaction.status == "expected",
            Transaction.superseded_by.is_(None),
            posted.status.in_(matching.MATCHABLE_STATUSES),
            posted.superseded_by.is_(None),
        )
    )
    return {match_fingerprint(expected, posted_id) for expected, posted_id in pairs}


def detector_evidence_current(row: Suggestion) -> bool:
    """Check detector-owned evidence without mutating a GET or an agent draft.

    The nightly detector refreshes from the ledger and audits withdrawals.
    This read-time gate also hides a lapsed card before that job runs.
    """
    if row.kind != KIND_SCHEDULE or row.detector != "recurrence_v2":
        return True
    from purser.core import recurrence

    evidence = (row.evidence or {}).get("occurrences", [])
    occurrences = [
        recurrence.Occurrence(
            date=date_value_type.fromisoformat(o["date"]), amount=Decimal(str(o["amount"]))
        )
        for o in evidence
    ]
    return recurrence.detect(occurrences, today=clock.ledger_today()) is not None


def withdraw_missing_schedules(
    session: Session,
    *,
    fingerprints: set[str],
    account_id: uuid.UUID | None,
    counterparty_id: uuid.UUID | None,
    actor_token_id: uuid.UUID | None,
) -> None:
    """Retire detector-owned proposals absent from the completed scan's scope."""
    query = select(Suggestion).where(
        Suggestion.kind == KIND_SCHEDULE,
        Suggestion.status == STATUS_OPEN,
        Suggestion.detector == "recurrence_v2",
        Suggestion.fingerprint.not_in(fingerprints),
    )
    if account_id is not None:
        query = query.where(Suggestion.subject_account_id == account_id)
    if counterparty_id is not None:
        query = query.where(Suggestion.counterparty_account_id == counterparty_id)
    for row in session.scalars(query):
        _close_card(
            session,
            row,
            status=STATUS_WITHDRAWN,
            actor_token_id=actor_token_id,
            reason="recurring pattern no longer supported by current history",
        )


def _live_card_clause(session: Session) -> Any:
    stale = [
        row.id
        for row in session.scalars(
            select(Suggestion).where(
                Suggestion.kind == KIND_SCHEDULE,
                Suggestion.status == STATUS_OPEN,
                Suggestion.detector == "recurrence_v2",
            )
        )
        if not detector_evidence_current(row)
    ]
    return and_(
        or_(
            Suggestion.kind != KIND_MATCH,
            Suggestion.fingerprint.in_(_live_match_fingerprints(session)),
        ),
        Suggestion.id.not_in(stale),
    )


def list_suggestions(
    session: Session,
    *,
    status: str = STATUS_OPEN,
    kind: str | None = None,
    account_ref: str | None = None,
    include_snoozed: bool = False,
    limit: int = LIST_MAX_ROWS,
    offset: int = 0,
    sort: str = "confidence",
) -> list[Suggestion]:
    """One page of the inbox, by confidence (unscored rows last), then age;
    ``offset`` skips the rows of earlier pages — the order is total, so the
    pages never overlap or skip a row while nothing changes."""
    _check_kind(kind)
    _check_status(status)
    if offset < 0:
        raise SuggestionError("offset must not be negative")
    if sort not in {"confidence", "oldest"}:
        raise SuggestionError("sort must be confidence or oldest")
    order = [Suggestion.created_at, Suggestion.id]
    stmt = select(Suggestion).order_by(
        *([Suggestion.confidence.desc().nulls_last(), *order] if sort == "confidence" else order)
    )
    if status != "all":
        stmt = stmt.where(Suggestion.status == status)
        if status == STATUS_OPEN:
            stmt = stmt.where(_live_card_clause(session))
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
            or_(Suggestion.snooze_until.is_(None), Suggestion.snooze_until <= clock.ledger_today())
        )
    return list(session.execute(stmt.offset(offset).limit(min(limit, LIST_MAX_ROWS))).scalars())


def open_counts(session: Session, *, account_ref: str | None = None) -> dict[str, int]:
    """Open, un-snoozed rows of every kind, zero included: each kind's total
    beside the page the inbox lists, so a page never hides how many wait
    (#386 review)."""
    counts = dict.fromkeys(KINDS, 0)
    stmt = select(Suggestion.kind, func.count(Suggestion.id)).where(
        Suggestion.status == STATUS_OPEN,
        _live_card_clause(session),
        or_(Suggestion.snooze_until.is_(None), Suggestion.snooze_until <= clock.ledger_today()),
    )
    if account_ref is not None:
        account = ledger_service.resolve_account(session, account_ref)
        stmt = stmt.where(
            or_(
                Suggestion.subject_account_id == account.id,
                Suggestion.counterparty_account_id == account.id,
            )
        )
    for kind, count in session.execute(stmt.group_by(Suggestion.kind)):
        counts[kind] = int(count)
    return counts


def open_count(
    session: Session, *, account_id: uuid.UUID | None = None, kind: str | None = None
) -> int:
    """Open, un-snoozed rows — all kinds (the inbox), or one ``kind``: the
    forecast and the subscription radar count only schedule suggestions,
    since a transfer-link proposal changes no expected transaction."""
    _check_kind(kind)
    stmt = select(func.count(Suggestion.id)).where(
        Suggestion.status == STATUS_OPEN,
        or_(Suggestion.snooze_until.is_(None), Suggestion.snooze_until <= clock.ledger_today()),
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


def path_leaf_label(path: str) -> str:
    """A path no account carries any more (a deleted category, a system
    account such as ``equity.securities``) as words: its last label,
    humanized — the admin UI's ``pathLeafLabel``, so both read alike."""
    leaf = next((label for label in reversed(path.split(".")) if label), path)
    words = re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", leaf)).strip()
    return words[:1].upper() + words[1:] if words else path


class AccountNames:
    """Path -> the name a person knows an account or category by (#552). A
    headline reads accounts by name ("on Checking", never "on
    assets.checking"); the draft keeps the paths accept applies and tool
    calls take. Loaded on first use, once per response: build one and pass
    it to every row of a page."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_path: dict[str, str] | None = None

    def __call__(self, path: object) -> str:
        text = str(path) if path else ""
        if not text:
            return ""
        if self._by_path is None:
            rows = self._session.execute(select(Account.path, Account.name))
            self._by_path = {str(path): name for path, name in rows}
        return self._by_path.get(text) or path_leaf_label(text)


ESTIMATE_MARK = "\u2248"


def estimate_text(amount: Decimal) -> str:
    """An estimated amount in text (#556): "≈ 1200.00" — U+2248, a space,
    then the amount to the cent, a negative one with a real minus sign
    (U+2212): the mark the admin SPA and the widgets show too.
    ``privacy.amount_forms`` knows the U+2212 spelling, so a withheld amount
    is still removed."""
    cents = amount.quantize(Decimal("0.01"))
    text = str(abs(cents) if cents.is_zero() else cents)
    if text.startswith("-"):
        text = MINUS_SIGN + text[1:]
    return f"{ESTIMATE_MARK} {text}"


def headline(row: Suggestion, names: AccountNames, *, draft: dict[str, Any] | None = None) -> str:
    """One line an agent (or a card) can show — for a schedule who, cadence
    and how much; for a transfer link the pair and the payee it is scoped
    to; for a match the bill against the bank row and the variance; for a
    transfer pair the amount, where it left and arrived, and when.

    Accounts read by name (#552): people read what an agent relays, and they
    know "Checking", not ``assets.checking``. The draft beside it keeps the
    paths. Amounts stay plain decimal strings, the spellings the privacy
    scrubber removes when a call withholds amounts (``privacy.amount_forms``);
    an estimated schedule amount is marked as one (:func:`estimate_text`)."""
    draft = row.draft if draft is None else draft
    if row.kind == KIND_SPLIT:
        row_amount = Decimal(str(draft.get("amount"))).quantize(Decimal("0.01"))
        return (
            f"Split the {row_amount} row of {draft.get('date')} on {names(draft.get('account'))}"
            f" like last time ({len(draft.get('lines') or [])} lines)"
        )
    if row.kind == KIND_TRANSFER_PAIR:
        moved = Decimal(str(draft.get("amount"))).quantize(Decimal("0.01"))
        return (
            f"Link {moved} from {names(draft.get('from_account'))} ({draft.get('outgoing_date')})"
            f" to {names(draft.get('to_account'))} ({draft.get('incoming_date')}) as one transfer"
        )
    if row.kind == KIND_MATCH:
        cents = Decimal("0.01")
        expected = Decimal(str(draft.get("expected_amount"))).quantize(cents)
        posted = Decimal(str(draft.get("posted_amount"))).quantize(cents)
        variance = Decimal(str(draft.get("variance"))).quantize(cents)
        return (
            f"Match {draft.get('label')} ({expected} due {draft.get('expected_date')}) to the "
            f"{posted} bank row of {draft.get('posted_date')} on {names(draft.get('account'))}"
            f" — variance {variance}"
        )
    if row.kind == KIND_TRADE_RULE:
        # The text predicate stays in the draft (withheld like other bank
        # text), never in the headline.
        which = {"debit": "buy (money out)", "credit": "sell (money in)"}.get(
            str(draft.get("direction")), "buy or sell"
        )
        return (
            f"Mark {which} rows like the one you marked on {names(draft.get('account'))} as a"
            " securities trade, with a rule"
        )
    if row.kind == KIND_TRANSFER_LINK:
        return (
            f"Link transfers between {names(draft.get('from_account'))} and"
            f" {names(draft.get('to_account'))} when the payee is {draft.get('payee')}"
            " (or an alias)"
        )
    from purser.core import forecasting as core_forecasting

    cadence = core_forecasting.cadence_label(str(draft.get("rrule", "")))
    amount = draft.get("amount")
    if not amount:
        how_much = "amount from history"
    elif draft.get("amount_type") == "estimated":
        how_much = estimate_text(Decimal(str(amount)))
    else:
        how_much = str(Decimal(str(amount)).quantize(Decimal("0.01")))
    counterparty = draft.get("counterparty_account")
    with_whom = names(counterparty) if counterparty else draft.get("payee") or draft.get("name")
    return (
        f"{draft.get('name')}: {cadence} {how_much} on {names(draft.get('account'))}"
        f" with {with_whom}"
    )


def serialize(
    session: Session,
    row: Suggestion,
    *,
    with_evidence: bool = False,
    names: AccountNames | None = None,
) -> dict[str, Any]:
    """The compact shape shared by REST and MCP (guardrail 6: evidence is
    summarized unless asked for). Every kind keeps its evidence as
    ``occurrences`` — detected rows for a schedule, confirmed pairs for a
    transfer link, the bank row for a match, the candidate pair for a
    transfer pair — so the summary reads the same way for each. A page of
    rows shares one ``names`` (one account query for the page)."""
    draft = row.draft
    evidence = row.evidence or {}
    if row.kind == KIND_MATCH and row.status == STATUS_OPEN:
        match = _live_proposal_for(session, row)
        loaded = _match_rows(session, match) if match is not None else None
        parts = _match_card(match, *loaded) if match is not None and loaded else None
        if parts is not None:
            draft = parts[0].stored()
            evidence = parts[1]
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
    if "amount" in evidence:
        summary["latest_amount"] = evidence["amount"].get("latest")
        summary["price_changed"] = evidence["amount"].get("price_changed", False)
    if evidence.get("warning"):
        summary["warning"] = evidence["warning"]
    if evidence.get("note"):
        # An agent's proposal note (#250), kept encrypted with the evidence.
        summary["note"] = evidence["note"]
    out: dict[str, Any] = {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "source": row.source,
        "confidence": str(row.confidence) if row.confidence is not None else None,
        "headline": headline(row, names or AccountNames(session), draft=draft),
        "draft": draft,
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
