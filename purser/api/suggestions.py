"""Suggestions inbox API (issue #233, DESIGN decisions 57 and 67).

Read with `read`, decide with `write`. There is no create route: suggestions
are made by detection, by the liability-added hook, by `create_schedule`
with `propose=true`, by a manual transfer confirm (a `transfer_link`
suggestion), by the matcher (a `match` card mirroring each open match
proposal, decision 86) and by the matching pass after an import (a
`transfer_pair` card for each recent transfer candidate, decision 102) —
never by hand.
"""

import uuid
from datetime import date as date_type
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from purser import rules as rules_service
from purser import suggestions as service
from purser.admin.auth import ReadToken, WriteToken
from purser.api.matching import MatchOut
from purser.api.rules import RuleOut, _Accounts
from purser.api.schedules import ScheduleOut
from purser.db import get_session
from purser.ledger import LedgerError, NotFoundError
from purser.models import Match, Rule, Schedule

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/suggestions", tags=["suggestions"])

SuggestionKind = Literal["schedule", "transfer_link", "match", "transfer_pair"]
SuggestionStatus = Literal["open", "accepted", "rejected", "withdrawn"]


class SuggestionOut(BaseModel):
    id: uuid.UUID
    kind: SuggestionKind = Field(
        description=(
            "schedule: a recurring bill or transfer to expect (draft = a schedule). "
            "transfer_link: proposed by a manual transfer confirm (draft = a transfer_link "
            "rule for that account pair, scoped to the payee and its aliases). "
            "match: an open match proposal pairing an expected row with a bank row "
            "(draft = the pair; accept confirms it, reject rejects it for good). "
            "transfer_pair: a transfer candidate, two bank rows of exactly opposite amounts on "
            "two accounts that nothing links automatically (draft = the pair; accept confirms "
            "the transfer, reject remembers it is not one)."
        )
    )
    status: str = Field(
        description="open | accepted | rejected | withdrawn (closed by the system: the "
        "match proposal it mirrored was rescinded, carried or purged; the transfer pair "
        "stopped being a candidate or was linked automatically; a detected schedule "
        "pattern is no longer supported by current history)"
    )
    source: str = Field(description="detected | agent | account_added | confirm")
    confidence: str | None
    headline: str
    draft: dict[str, Any] = Field(
        description=(
            "The typed draft accept applies unchanged. schedule: name, account, "
            "counterparty_account, rrule, dtstart, amount_type, amount, estimate_basis, payee, "
            "category, autopost_policy. transfer_link: from_account, to_account, payee, "
            "payee_id, amount_exact, window_days, effective_from. match: expected_id, "
            "posted_id, account, label, expected_date, expected_amount, posted_date, "
            "posted_amount, variance (posted - expected). transfer_pair: outgoing_id, "
            "incoming_id, from_account, to_account, amount (moved, positive), outgoing_date, "
            "incoming_date, date_gap_days, outgoing_description, incoming_description."
        )
    )
    evidence_summary: dict[str, Any]
    evidence: dict[str, Any] | None = None
    snooze_until: date_type | None
    seen_count: int
    created_at: str
    decided_at: str | None
    reject_scope: str | None
    result_entity_type: str | None
    result_entity_id: uuid.UUID | None


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject", "snooze", "edit_then_accept"]
    edits: dict[str, Any] | None = Field(
        default=None,
        description=(
            "draft fields to change before applying (edit_then_accept). A schedule takes any "
            "draft field; a transfer_link takes amount_exact (a decimal string), window_days "
            "(0 to 14) and effective_from (YYYY-MM-DD) — its pair and payee are what it "
            "proposes."
        ),
    )
    note: str | None = None
    reject_scope: Literal["exact", "counterparty"] = Field(
        default="exact",
        description=(
            "exact = just this suggestion. counterparty = never suggest this (account, "
            "counterparty/payee) again — for a transfer_link, this account pair with any payee. "
            "A match and a transfer_pair reject exactly."
        ),
    )
    snooze_until: date_type | None = Field(default=None, description="default today + 30d")


class TransferPairDecisionOut(BaseModel):
    merged_transaction_id: uuid.UUID = Field(
        description="the ONE transfer transaction the two bank rows became"
    )
    suggestion_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "the payee-scoped transfer_link suggestion the confirm proposed (or refreshed) in "
            "the inbox, as POST /matching/confirm-transfer does (decision 67); null when it "
            "proposed none (no payee on either row, or a rule or rejection already covers it)"
        ),
    )


class DecisionOut(BaseModel):
    suggestion: SuggestionOut
    schedule: ScheduleOut | None = Field(
        default=None, description="the schedule an accept produced (active, materialized)"
    )
    rule: RuleOut | None = Field(
        default=None,
        description=(
            "the transfer_link rule an accept produced — a DRAFT. It links nothing until "
            "activated with POST /rules/{rule_id}/state {state: active, confirm_links: "
            "projected_links}."
        ),
    )
    projected_links: int | None = Field(
        default=None,
        description=(
            "transfer_link accepts: how many historical pairs activating that rule would link "
            "now — the count its activation asks you to confirm."
        ),
    )
    match: MatchOut | None = Field(
        default=None,
        description="match decisions: the match the decision produced (confirmed or rejected)",
    )
    transfer: TransferPairDecisionOut | None = Field(
        default=None,
        description=(
            "transfer_pair accepts: the transfer the pair merged into and the transfer_link "
            "suggestion the confirm proposed"
        ),
    )


def _handle(exc: LedgerError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if "requires write scope" in str(exc):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def suggestion_out(session: Session, row: Any, *, with_evidence: bool = False) -> SuggestionOut:
    return SuggestionOut(**service.serialize(session, row, with_evidence=with_evidence))


@router.get("")
def list_suggestions(
    token: ReadToken,
    session: DbSession,
    status_: Annotated[
        Literal["open", "accepted", "rejected", "withdrawn", "all"], Query(alias="status")
    ] = "open",
    kind: SuggestionKind | None = None,
    account: Annotated[str | None, Query(description="account id or path (either side)")] = None,
    include_snoozed: bool = False,
    sort: Literal["confidence", "oldest"] = "confidence",
    limit: Annotated[int, Query(ge=1, le=service.LIST_MAX_ROWS)] = service.LIST_MAX_ROWS,
    offset: Annotated[
        int, Query(ge=0, description="rows of earlier pages to skip, for the next page")
    ] = 0,
) -> list[SuggestionOut]:
    """The inbox: open suggestions by confidence (snoozed ones hidden until
    their date unless include_snoozed). `kind` narrows it to schedules, to
    the transfer-link rules manual confirms proposed, to the matcher's open
    match proposals, or to the recent transfer candidates (transfer_pair).
    At most `limit` rows a page; GET /suggestions/counts says how many of
    each kind there are, and `offset` reaches the next page."""
    try:
        rows = service.list_suggestions(
            session,
            status=status_,
            kind=kind,
            account_ref=account,
            include_snoozed=include_snoozed,
            limit=limit,
            offset=offset,
            sort=sort,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    return [suggestion_out(session, r) for r in rows]


class SuggestionCountsOut(BaseModel):
    """Open suggestions the inbox shows, snoozed ones left out: the total
    and each kind's count."""

    total: int
    schedule: int
    transfer_link: int
    match: int
    transfer_pair: int


@router.get("/counts")
def suggestion_counts(
    token: ReadToken, session: DbSession, account: str | None = None
) -> SuggestionCountsOut:
    """How many open suggestions of each kind wait (snoozed ones hidden
    until their date, as in the inbox): the totals beside the pages GET
    /suggestions lists, so a page never hides a kind (#386 review)."""
    try:
        counts = service.open_counts(session, account_ref=account)
    except LedgerError as exc:
        raise _handle(exc) from exc
    return SuggestionCountsOut(total=sum(counts.values()), **counts)


@router.get("/{suggestion_id}")
def get_suggestion(suggestion_id: uuid.UUID, token: ReadToken, session: DbSession) -> SuggestionOut:
    """One suggestion with its full evidence (the occurrences behind it, or
    for a transfer_link the confirmed pairs)."""
    try:
        row = service.get_suggestion(session, suggestion_id)
    except LedgerError as exc:
        raise _handle(exc) from exc
    return suggestion_out(session, row, with_evidence=True)


@router.post("/{suggestion_id}/decide")
def decide(
    suggestion_id: uuid.UUID, body: DecisionRequest, token: WriteToken, session: DbSession
) -> DecisionOut:
    """accept / edit_then_accept apply the draft: a schedule suggestion
    creates the schedule (active, materialized); a transfer_link suggestion
    creates the rule as a DRAFT and returns `projected_links` — activate it
    through POST /rules/{rule_id}/state with that count confirmed. reject is
    durable and scoped; snooze hides it until a date. Nothing ever
    auto-accepts, and nothing here activates a rule. A match suggestion's
    accept confirms the proposal (marking its bank row reviewed) and reject
    rejects it for good; it has nothing to edit. If its proposal is already
    gone the suggestion is withdrawn and the call answers 422. A
    transfer_pair suggestion's accept confirms the transfer (as POST
    /matching/confirm-transfer does: `transfer` names the merged row and the
    transfer_link suggestion it proposed) and reject remembers that the pair
    is not a transfer — the automatic link paths leave it alone too; nothing
    to edit, and a pair whose rows moved on withdraws it with a 422."""
    try:
        row, created = service.decide(
            session,
            suggestion_id=suggestion_id,
            decision=body.decision,
            token=token,
            edits=body.edits,
            note=body.note,
            reject_scope=body.reject_scope,
            snooze_until=body.snooze_until,
        )
    except service.SuggestionWithdrawn as exc:
        session.commit()  # the withdrawal stands; the card leaves the inbox
        raise _handle(exc) from exc
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    out = DecisionOut(suggestion=suggestion_out(session, row))
    if isinstance(created, Schedule):
        out.schedule = ScheduleOut.from_model(created)
    elif isinstance(created, Rule):
        out.rule = RuleOut.from_model(created, _Accounts(session))
        out.projected_links = rules_service.project_links(session, created)
    elif isinstance(created, Match):
        out.match = MatchOut.from_model(created)
    elif isinstance(created, service.TransferPairConfirmed):
        out.transfer = TransferPairDecisionOut(
            merged_transaction_id=created.merged.id,
            suggestion_id=created.proposed.id if created.proposed is not None else None,
        )
    return out
