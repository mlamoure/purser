"""Matching API: run, review queue, unmatched reports, transfer confirm."""

import uuid
from datetime import date as date_type
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from purser import matching
from purser.admin.auth import ReadToken, WriteToken
from purser.core import matching as core_matching
from purser.db import get_session
from purser.ledger import LedgerError, NotFoundError
from purser.models import Match, Posting, Transaction

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/matching", tags=["matching"])


class MatchOut(BaseModel):
    id: uuid.UUID
    expected_id: uuid.UUID
    posted_id: uuid.UUID
    score: float
    variance: Decimal | None
    variance_ratio: Decimal | None
    state: str
    # Context of the expected side, so proposal surfaces (register badges,
    # dashboard) can render without a second query per match.
    expected_date: date_type | None = None
    expected_amount: Decimal | None = None
    expected_payee: str | None = None
    expected_description: str | None = None

    @classmethod
    def from_model(
        cls,
        m: Match,
        expected_amount: Decimal | None = None,
        *,
        expected: Transaction | None = None,
    ) -> "MatchOut":
        expected_date: date_type | None = None
        expected_payee: str | None = None
        expected_description: str | None = None
        if expected is not None:
            real = [
                p for p in expected.postings if p.account is not None and not p.account.is_virtual
            ]
            if expected_amount is None and len(real) == 1:
                expected_amount = Decimal(real[0].amount)
            expected_date = expected.date
            expected_payee = expected.payee.name if expected.payee else None
            expected_description = expected.description
        ratio = None
        if m.variance is not None and expected_amount:
            ratio = core_matching.variance_ratio(expected_amount, Decimal(m.variance))
        return cls(
            id=m.id,
            expected_id=m.expected_id,
            posted_id=m.posted_id,
            score=m.score,
            variance=m.variance,
            variance_ratio=ratio,
            state=m.state,
            expected_date=expected_date,
            expected_amount=expected_amount,
            expected_payee=expected_payee,
            expected_description=expected_description,
        )


class UnmatchedOut(BaseModel):
    transaction_id: uuid.UUID
    date: date_type
    amount: Decimal | None
    payee: str | None
    description: str | None
    aging_days: int
    has_proposed_match: bool
    proposed_posted_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "the bank row an open match proposal pairs this expected with; "
            "POST /matching/match with this pair confirms the proposal (decision 76)"
        ),
    )
    proposal_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "the open match proposal's id (POST /matching/{match_id}/confirm or /reject); "
            "it also waits in the suggestions inbox as a kind=match card (decision 86)"
        ),
    )


class TransferCandidateOut(BaseModel):
    outgoing_id: uuid.UUID
    incoming_id: uuid.UUID
    amount: Decimal
    date_gap_days: int
    outgoing_description: str | None
    incoming_description: str | None


class ManualMatchRequest(BaseModel):
    expected_id: uuid.UUID
    posted_id: uuid.UUID


class ConfirmTransferRequest(BaseModel):
    outgoing_id: uuid.UUID
    incoming_id: uuid.UUID
    remainder_category: str | None = Field(
        default=None,
        description=(
            "split loan payment (DESIGN §3.3, decision 62): an expense category id "
            "or path (e.g. expenses.fees.interest). The two bank legs keep their exact, "
            "unequal amounts (the incoming principal positive and smaller than the "
            "outgoing cash) and the difference books to this category. Omit for an "
            "ordinary transfer (opposite amounts)."
        ),
    )


def _handle(exc: LedgerError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if "requires write scope" in str(exc):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("/run")
def run_matching(token: WriteToken, session: DbSession) -> dict[str, int]:
    """Score open expected transactions against imported reality."""
    stats = matching.run_matching(session, actor_token_id=token.id)
    session.commit()
    return stats


@router.get("/proposed")
def list_proposed(token: ReadToken, session: DbSession) -> list[MatchOut]:
    rows = list(
        session.execute(
            select(Match).where(Match.state == "proposed").order_by(Match.created_at)
        ).scalars()
    )
    expected_by_id: dict[uuid.UUID, Transaction] = {}
    if rows:
        expected_by_id = {
            t.id: t
            for t in session.execute(
                select(Transaction)
                .options(
                    selectinload(Transaction.postings).selectinload(Posting.account),
                    selectinload(Transaction.payee),
                )
                .where(Transaction.id.in_({m.expected_id for m in rows}))
            ).scalars()
        }
    return [MatchOut.from_model(m, expected=expected_by_id.get(m.expected_id)) for m in rows]


@router.get("/unmatched")
def list_unmatched(
    token: ReadToken,
    session: DbSession,
    direction: Annotated[Literal["expected", "posted"], Query()] = "expected",
) -> list[UnmatchedOut]:
    """Both directions with aging: expected-but-missing (late bills) and
    posted-but-unexpected (new-spend triage)."""
    try:
        rows = matching.list_unmatched(session, direction=direction)
    except LedgerError as exc:
        raise _handle(exc) from exc
    return [UnmatchedOut(**r) for r in rows]


@router.post("/{match_id}/confirm")
def confirm_match(match_id: uuid.UUID, token: WriteToken, session: DbSession) -> MatchOut:
    try:
        match = matching.confirm_match(session, match_id=match_id, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return MatchOut.from_model(match)


@router.post("/{match_id}/reject")
def reject_match(match_id: uuid.UUID, token: WriteToken, session: DbSession) -> MatchOut:
    try:
        match = matching.reject_match(session, match_id=match_id, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return MatchOut.from_model(match)


@router.post("/match")
def manual_match(body: ManualMatchRequest, token: WriteToken, session: DbSession) -> MatchOut:
    """Mark an expected row paid by a bank row (write scope). When the matcher
    already proposed exactly this pair (the unmatched report's
    `proposed_posted_id`), the proposal is confirmed in place; a bank row
    proposed for a different expected is refused, naming it (decision 76)."""
    try:
        match = matching.manual_match(
            session, expected_id=body.expected_id, posted_id=body.posted_id, token=token
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return MatchOut.from_model(match)


@router.post("/{match_id}/unmatch")
def unmatch(match_id: uuid.UUID, token: WriteToken, session: DbSession) -> dict[str, str]:
    try:
        clone = matching.unmatch(session, match_id=match_id, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return {"recreated_expected_id": str(clone.id)}


@router.get("/transfer-candidates")
def transfer_candidates(token: ReadToken, session: DbSession) -> list[TransferCandidateOut]:
    """Opposite-amount pairs across accounts — candidates only, never merged
    without confirm."""
    rows = matching.list_transfer_candidates(session)
    session.commit()  # persist first-seen candidate events
    return [TransferCandidateOut(**r) for r in rows]


@router.post("/confirm-transfer")
def confirm_transfer(
    body: ConfirmTransferRequest, token: WriteToken, session: DbSession
) -> dict[str, str]:
    """Merge a one-sided pair into ONE transfer (write scope). With
    `remainder_category` it is a split loan payment: exact cash and principal
    legs plus one interest/fee posting; the response then also carries
    `remainder` and `remainder_category`. An ordinary confirm also proposes a
    payee-scoped transfer_link rule in the suggestions inbox (never active
    until accepted and its link count confirmed); the response then carries
    that suggestion's `suggestion_id`."""
    try:
        merged, suggestion = matching.confirm_transfer_and_propose(
            session,
            outgoing_id=body.outgoing_id,
            incoming_id=body.incoming_id,
            token=token,
            remainder_category_ref=body.remainder_category,
        )
        result = matching.transfer_confirmation(merged, suggestion)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return result
