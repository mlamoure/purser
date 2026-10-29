"""Matching service: expected vs imported reality (DESIGN §5).

On match the expected is voided with a superseded_by link, the posted row is
truth (posted amount wins), and the matches row records score + variance.
Transfer merges NEVER happen without an explicit confirm.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from purser import audit, balances, events
from purser import ledger as ledger_service
from purser import review as review_service
from purser import suggestions as suggestions_service
from purser.admin import security
from purser.core import ledger as core
from purser.core import matching as core_matching
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

MATCHABLE_STATUSES = ["pending", "posted"]
# A match that holds its rows: at most one per expected and per posted row
# (matches_expected_live_uniq / matches_posted_live_uniq).
LIVE_MATCH_STATES = ["proposed", "confirmed"]


def _estimate_probe(session: Session, expected: Transaction) -> tuple[Schedule, Decimal] | None:
    """Snapshot an estimated schedule's instance amount BEFORE a match-state
    change so the post-flush shift can emit recurring.price_changed (#74)."""
    if expected.schedule_id is None:
        return None
    schedule = session.get(Schedule, expected.schedule_id)
    if schedule is None or schedule.amount_type != "estimated":
        return None
    from purser import schedules as schedules_service

    return schedule, schedules_service.instance_amount(session, schedule)


def _emit_price_change(
    session: Session, probe: tuple[Schedule, Decimal] | None, *, match: Match
) -> None:
    """Confirmed-match variance shifting a schedule's estimate IS a price
    change — the structural advantage issue #74 leans on. Feeds #77."""
    if probe is None:
        return
    schedule, old_estimate = probe
    from purser import schedules as schedules_service

    new_estimate = schedules_service.instance_amount(session, schedule)
    if new_estimate == old_estimate:
        return
    payee = None
    if schedule.payee_id is not None:
        payee_row = session.get(Payee, schedule.payee_id)
        payee = payee_row.name if payee_row else None
    events.emit(
        session,
        type_="recurring.price_changed",
        entity_type="schedule",
        entity_id=schedule.id,
        payload={
            "schedule_id": str(schedule.id),
            "payee": payee or schedule.name,
            "old_estimate": str(old_estimate),
            "new_estimate": str(new_estimate),
            "delta": str(abs(new_estimate) - abs(old_estimate)),
            "match_id": str(match.id),
        },
    )


def _real_leg(tx: Transaction) -> Posting | None:
    real = [p for p in tx.postings if not p.account.is_virtual]
    return real[0] if len(real) == 1 else None


def _real_legs(tx: Transaction) -> list[Posting]:
    return [p for p in tx.postings if not p.account.is_virtual]


def _perspective_leg(tx: Transaction, account_id: uuid.UUID | None) -> Posting | None:
    """The leg matching sees a row through (#232): the one on ``account_id``
    when given; otherwise a one-sided row's only real leg, and for a two-leg
    row with no perspective its outgoing (negative) leg — the payer's view."""
    real = _real_legs(tx)
    if account_id is not None:
        return next((p for p in real if p.account_id == account_id), None)
    if len(real) == 1:
        return real[0]
    if len(real) == 2:
        return min(real, key=lambda p: Decimal(p.amount))
    return None


def _match_input(
    tx: Transaction, perspective_account_id: uuid.UUID | None = None
) -> core_matching.MatchInput | None:
    leg = _perspective_leg(tx, perspective_account_id)
    if leg is None:
        return None
    real = _real_legs(tx)
    counterparty = (
        next((p.account_id for p in real if p.id != leg.id), None) if len(real) == 2 else None
    )
    # normalized_name is gone (#137: replaced by the blind index, an HMAC that
    # is NOT normalized text) — recompute the normalized form from the decrypted
    # name for the payee-similarity term of the match score.
    payee = (
        core.normalize_payee(tx.payee.name)
        if tx.payee
        else core.normalize_payee(tx.description or "")
    )
    return core_matching.MatchInput(
        id=tx.id,
        date=tx.date,
        amount=Decimal(leg.amount),
        normalized_payee=payee,
        account_id=leg.account_id,
        counterparty_account_id=counterparty,
    )


def _load_tx(session: Session, tx_id: uuid.UUID) -> Transaction:
    tx = session.execute(
        select(Transaction)
        .options(
            selectinload(Transaction.postings).selectinload(Posting.account),
            selectinload(Transaction.payee),
        )
        .where(Transaction.id == tx_id)
    ).scalar_one_or_none()
    if tx is None:
        raise NotFoundError(f"transaction {tx_id} not found")
    return tx


def _expected_transactions(session: Session) -> list[Transaction]:
    return list(
        session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(Transaction.status == "expected", Transaction.superseded_by.is_(None))
        ).scalars()
    )


def _candidates_for(
    session: Session, account_id: uuid.UUID, around: date_type
) -> list[Transaction]:
    window = timedelta(days=core_matching.DATE_WINDOW_DAYS)
    already_matched = select(Match.posted_id).where(Match.state.in_(["proposed", "confirmed"]))
    # Bank reality is origin=import, plus origin=system for the rows the
    # system re-derives from it (unlink clones carry the bank line's content
    # hash). Merged transfers are structural — two real legs — not an origin:
    # the auto-linker's merges are origin=system, a human confirm's carry the
    # confirming actor. A card payment expected on checking must pair with
    # the merge, or it is never matched and double-counts in the forecast
    # (#232).
    two_real_legs = (
        select(Posting.transaction_id)
        .join(Account, Account.id == Posting.account_id)
        .where(Account.is_virtual.is_(False))
        .group_by(Posting.transaction_id)
        .having(func.count(Posting.id) == 2)
    )
    # Follow complete correction pairs, never their reversal half. Origin is
    # the actor of a correction, not evidence of whether its root was a bank
    # line (#419). UNION deduplicates structural transfer roots and descendants.
    bank_heads = (
        select(Transaction.id)
        .where(
            or_(
                Transaction.origin == "import",
                and_(Transaction.origin == "system", Transaction.content_hash.is_not(None)),
                Transaction.id.in_(two_real_legs),
            )
        )
        .cte("bank_corrections", recursive=True)
    )
    reversal = aliased(Transaction)
    replacement = aliased(Transaction)
    bank_heads = bank_heads.union(
        select(replacement.id)
        .join(reversal, replacement.reverses_id == reversal.id)
        .join(bank_heads, reversal.reverses_id == bank_heads.c.id)
    )
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    return list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(
                Transaction.status.in_(MATCHABLE_STATUSES),
                Transaction.id.in_(select(bank_heads.c.id)),
                Transaction.id.not_in(reversed_ids),
                Transaction.superseded_by.is_(None),
                Transaction.schedule_id.is_(None),
                Transaction.id.not_in(already_matched),
                Posting.account_id == account_id,
                Transaction.date >= around - window,
                Transaction.date <= around + window,
            )
            .distinct()
        ).scalars()
    )


def _best_for_expected(session: Session, expected: Transaction) -> core_matching.Scored | None:
    """Score the expected against candidates on each of its real accounts —
    a two-leg expected transfer can be settled by a merged transfer or by the
    bank row on either side — and keep the best (#232).

    A bank row a person rejected for this expected's slot is never a
    candidate again, nor the row that superseded it (its posted successor,
    or the transfer it merged into): that is the rejection memory both the
    auto and the proposed tiers honour (decision 86). An explicit ``match``
    can still pair them."""
    rejected = suggestions_service.rejected_bank_rows(session, expected)
    if rejected:
        rejected |= {
            successor
            for successor in session.execute(
                select(Transaction.superseded_by).where(Transaction.id.in_(rejected))
            ).scalars()
            if successor is not None
        }
    best: core_matching.Scored | None = None
    for leg in _real_legs(expected):
        expected_input = _match_input(expected, leg.account_id)
        if expected_input is None:
            continue
        candidates = [
            ci
            for c in _candidates_for(session, leg.account_id, expected.date)
            if c.id not in rejected and (ci := _match_input(c, leg.account_id)) is not None
        ]
        scored = core_matching.best_match(expected_input, candidates)
        if scored is not None and (best is None or scored.score > best.score):
            best = scored
    return best


def _match_expected(session: Session, expected: Transaction) -> str | None:
    """Score one open expected and act on its tier — the matcher's single
    decision, shared by the nightly pass and the re-match a transfer merge
    triggers (decision 65). Returns the stats key it counts toward, or None
    when there is nothing to count (no real leg, or its live match stands)."""
    if not _real_legs(expected):
        return None
    best = _best_for_expected(session, expected)
    if best is None:
        return "unmatched"
    live = list(
        session.execute(
            select(Match).where(
                Match.expected_id == expected.id,
                Match.state.in_(LIVE_MATCH_STATES),
            )
        ).scalars()
    )
    if any(m.posted_id == best.candidate_id for m in live):
        return None
    if best.tier is core_matching.Tier.AUTO:
        _execute_match(
            session,
            expected=expected,
            posted_id=best.candidate_id,
            score=best.score,
            variance=best.variance,
            token=None,
            auto=True,
        )
        return "auto_matched"
    if live:
        # An open proposal already exists for this expected; never stack a
        # competing one (matches_expected_live_uniq). Review resolves it.
        return None
    match = Match(
        expected_id=expected.id,
        posted_id=best.candidate_id,
        score=best.score,
        variance=best.variance,
        state="proposed",
    )
    session.add(match)
    session.flush()
    events.emit(
        session,
        type_="match.proposed",
        entity_type="match",
        entity_id=match.id,
        payload={
            "expected_id": str(expected.id),
            "posted_id": str(best.candidate_id),
            "score": best.score,
            "variance": str(best.variance),
        },
    )
    # The inbox mirrors the proposal (decision 86); the matches row stays the
    # source of truth.
    suggestions_service.upsert_match_suggestion(session, match)
    return "proposed"


def proposed_settlements(
    session: Session, *, account_id: uuid.UUID | None, expected_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """The expected rows an open match proposal settles pending confirmation
    (decision 76), as ``{expected_id: bank row id}``.

    A proposal settles its expected when the bank row it names is live —
    pending or posted, not superseded — because such a row is already in the
    account's balance: forecasting the expected as well would subtract the
    money twice. Given ``account_id`` the bank row must also post to that
    account, and the account's opening balance must be built from postings:
    a valuation account's figure is a stated value that no bank row moves,
    so nothing is settled there. ``account_id=None`` accepts any live bank
    row (the digest's week of bills).

    The ``matches`` row is the source of truth — never the suggestions
    inbox: a snoozed card must not move a forecast. A rejected proposal
    settles nothing, so its expected is due again on the next read."""
    ids = list(expected_ids)
    if not ids:
        return {}
    query = (
        select(Match.expected_id, Match.posted_id)
        .join(Transaction, Transaction.id == Match.posted_id)
        .where(
            Match.state == "proposed",
            Match.expected_id.in_(ids),
            Transaction.status.in_(MATCHABLE_STATUSES),
            Transaction.superseded_by.is_(None),
        )
    )
    if account_id is not None:
        from purser import networth

        if account_id in networth.valuation_balances(session, [account_id]):
            return {}
        on_account = select(Posting.transaction_id).where(Posting.account_id == account_id)
        query = query.where(Match.posted_id.in_(on_account))
    return {expected: posted for expected, posted in session.execute(query).all()}


def run_matching(session: Session, *, actor_token_id: uuid.UUID | None = None) -> dict[str, int]:
    """Score every open expected against import reality; auto-execute the
    confident tier, queue the middle tier for review."""
    stats = {"auto_matched": 0, "proposed": 0, "unmatched": 0}
    for expected in _expected_transactions(session):
        outcome = _match_expected(session, expected)
        if outcome is not None:
            stats[outcome] += 1
    # Cards for every open proposal (the backfill, and held legs whose window
    # passed) and none for a proposal that is gone (decision 86).
    suggestions_service.sync_match_suggestions(session, actor_token_id=actor_token_id)
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="matching.run",
        entity_type="system",
        after=dict(stats),
    )
    return stats


def _execute_match(
    session: Session,
    *,
    expected: Transaction,
    posted_id: uuid.UUID,
    score: float,
    variance: Decimal,
    token: ApiToken | None,
    auto: bool,
    match: Match | None = None,
    via_suggestion: uuid.UUID | None = None,
) -> Match:
    """Expected superseded (void + link), posted is truth, variance recorded.

    ``token`` is whoever decided the match — a confirmed proposal or a manual
    match — and ``None`` the matcher's own auto-confirm. A token-bearing
    confirm is a review of the bank row (decision 47, #286): whoever says this
    row settles that bill has looked at it, so it leaves the unreviewed set
    ``via='match'``. An auto-confirm is automation touching the row, which
    never marks it."""
    decided_by = token.id if token is not None else None
    # Validate LIVE state — a match decided against rows that have since
    # moved on (voided originals, replayed confirms) is stale, not actionable.
    if expected.status != "expected":
        raise LedgerError(f"expected transaction is {expected.status}; match is stale")
    posted = session.get(Transaction, posted_id)
    if posted is None:
        raise NotFoundError(f"transaction {posted_id} not found")
    if posted.status not in MATCHABLE_STATUSES:
        raise LedgerError(f"posted transaction is {posted.status}; match is stale")
    estimate_probe = _estimate_probe(session, expected)

    # Rescind competing proposals on either side before this match goes live —
    # the partial unique indexes allow one live match per expected/posted row.
    sibling_conditions = [
        Match.state == "proposed",
        or_(Match.expected_id == expected.id, Match.posted_id == posted_id),
    ]
    if match is not None:
        sibling_conditions.append(Match.id != match.id)
    siblings = list(session.execute(select(Match).where(*sibling_conditions)).scalars())
    if siblings:
        for sibling in siblings:
            sibling.state = "rejected"
            sibling.decided_by = decided_by
        session.flush()
        audit.record(
            session,
            actor_token_id=decided_by,
            action="match.rescind",
            entity_type="match",
            after={
                "expected_id": str(expected.id),
                "posted_id": str(posted_id),
                "rescinded_match_ids": [str(s.id) for s in siblings],
            },
        )
        for sibling in siblings:
            suggestions_service.on_proposal_closed(
                session,
                sibling,
                outcome=suggestions_service.STATUS_WITHDRAWN,
                actor_token_id=decided_by,
                reason="sibling_confirmed",
            )

    if match is None:
        match = Match(
            expected_id=expected.id,
            posted_id=posted_id,
            score=score,
            variance=variance,
            state="confirmed",
            decided_by=decided_by,
        )
        session.add(match)
    else:
        match.state = "confirmed"
        match.decided_by = decided_by
        # A proposal confirmed: its card is accepted by whoever confirmed it,
        # unless that card is the very one being decided (decision 86).
        suggestions_service.on_proposal_closed(
            session,
            match,
            outcome=(
                suggestions_service.STATUS_ACCEPTED
                if token is not None
                else suggestions_service.STATUS_WITHDRAWN
            ),
            actor_token_id=decided_by,
            reason="confirmed",
            via_suggestion=via_suggestion,
        )
    expected.status = "void"
    expected.superseded_by = posted_id
    session.flush()
    if expected.schedule_id is not None:
        # The matched slot stays filled (occurrence_date kept); the schedule's
        # next_due moves to the next live instance (#231).
        from purser import schedules as schedules_service

        schedules_service.touch_instance(session, expected, pin=False)
    audit.record(
        session,
        actor_token_id=decided_by,
        action="match.confirm",
        entity_type="match",
        entity_id=match.id,
        after={
            "expected_id": str(expected.id),
            "posted_id": str(posted_id),
            "score": score,
            "variance": str(variance),
            "auto": auto,
        },
    )
    events.emit(
        session,
        type_="match.confirmed",
        entity_type="match",
        entity_id=match.id,
        payload={"auto": auto, "variance": str(variance), "score": score},
    )
    review_service.mark_if_in_scope(session, tx=posted, token=token, via=review_service.VIA_MATCH)

    expected_input = _match_input(expected)  # payer's view for a two-leg expected
    if expected_input is not None and core_matching.exceeds_variance_threshold(
        expected_input.amount, variance
    ):
        ratio = core_matching.variance_ratio(expected_input.amount, variance)
        events.emit(
            session,
            type_="variance.threshold",
            entity_type="match",
            entity_id=match.id,
            payload={
                "expected_amount": str(expected_input.amount),
                "variance": str(variance),
                "ratio": str(ratio),
            },
        )
    _emit_price_change(session, estimate_probe, match=match)
    return match


def get_match(session: Session, match_id: uuid.UUID) -> Match:
    match = session.get(Match, match_id)
    if match is None:
        raise NotFoundError(f"match {match_id} not found")
    return match


def confirm_match(
    session: Session,
    *,
    match_id: uuid.UUID,
    token: ApiToken,
    via_suggestion: uuid.UUID | None = None,
) -> Match:
    """Confirm a proposal. ``via_suggestion`` is the inbox card deciding it,
    which closes itself; any other path accepts the card here."""
    if not security.can_write(token.scopes):
        raise LedgerError("confirming a match requires write scope")
    match = get_match(session, match_id)
    if match.state != "proposed":
        raise LedgerError(f"match is {match.state}, not proposed")
    expected = _load_tx(session, match.expected_id)
    return _execute_match(
        session,
        expected=expected,
        posted_id=match.posted_id,
        score=match.score,
        variance=Decimal(match.variance) if match.variance is not None else Decimal(0),
        token=token,
        auto=False,
        match=match,
        via_suggestion=via_suggestion,
    )


def reject_match(
    session: Session,
    *,
    match_id: uuid.UUID,
    token: ApiToken | None,
    via_suggestion: uuid.UUID | None = None,
) -> Match:
    """``token=None`` is the system actor (the guarded retired-rows purge
    clears a doomed row's proposals this way, #284); scope gating applies to
    token-bearing callers only — ``unlink_transfer``'s convention.

    A token's rejection is durable: its card becomes ``rejected`` (created
    so when the proposal had none), and the matcher never proposes that
    pair again (decision 86). The purge's is a withdrawal."""
    if token is not None and not security.can_write(token.scopes):
        raise LedgerError("rejecting a match requires write scope")
    match = get_match(session, match_id)
    if match.state != "proposed":
        raise LedgerError(f"match is {match.state}, not proposed")
    match.state = "rejected"
    match.decided_by = token.id if token else None
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="match.reject",
        entity_type="match",
        entity_id=match.id,
        after={"expected_id": str(match.expected_id), "posted_id": str(match.posted_id)},
    )
    suggestions_service.on_proposal_closed(
        session,
        match,
        outcome=(
            suggestions_service.STATUS_REJECTED
            if token is not None
            else suggestions_service.STATUS_WITHDRAWN
        ),
        actor_token_id=token.id if token else None,
        reason="rejected" if token is not None else "purged",
        via_suggestion=via_suggestion,
    )
    return match


def manual_match(
    session: Session, *, expected_id: uuid.UUID, posted_id: uuid.UUID, token: ApiToken
) -> Match:
    if not security.can_write(token.scopes):
        raise LedgerError("matching requires write scope")
    expected = _load_tx(session, expected_id)
    posted = _load_tx(session, posted_id)
    if expected.status != "expected":
        raise LedgerError("expected transaction is not in expected status")
    if posted.status not in MATCHABLE_STATUSES:
        raise LedgerError("candidate must be pending or posted")
    live = session.execute(
        select(Match).where(Match.posted_id == posted.id, Match.state.in_(LIVE_MATCH_STATES))
    ).scalar_one_or_none()
    if live is not None and live.state == "proposed" and live.expected_id == expected.id:
        # The matcher already proposed this very pair: marking it paid is
        # confirming that proposal (decision 76) — an agent has no other way
        # to decide one, and the proposal itself holds the bank row.
        return _execute_match(
            session,
            expected=expected,
            posted_id=posted.id,
            score=live.score,
            variance=Decimal(live.variance) if live.variance is not None else Decimal(0),
            token=token,
            auto=False,
            match=live,
        )
    if live is not None and live.state == "proposed":
        raise LedgerError(
            "posted transaction already has a live match: it is proposed for expected "
            f"{live.expected_id}; decide that proposal first"
        )
    if live is not None:
        raise LedgerError("posted transaction already has a live match")
    # View both rows through an account they share (#232): a two-leg expected
    # settles against a merged transfer or against the bank row on either side.
    shared = {p.account_id for p in _real_legs(expected)} & {
        p.account_id for p in _real_legs(posted)
    }
    if not shared:
        raise LedgerError("the two transactions share no real account")
    perspective = min(shared, key=str)
    e_input, p_input = _match_input(expected, perspective), _match_input(posted, perspective)
    if e_input is None or p_input is None:
        raise LedgerError("both transactions need a real-account leg")
    scored = core_matching.score(e_input, p_input)
    return _execute_match(
        session,
        expected=expected,
        posted_id=posted.id,
        score=scored.score,
        variance=p_input.amount - e_input.amount,
        token=token,
        auto=False,
    )


def unmatch(session: Session, *, match_id: uuid.UUID, token: ApiToken | None) -> Transaction:
    """Undo a confirmed match. Void is terminal, so the superseded expected is
    not resurrected — a fresh expected clone is created with lineage.

    ``token=None`` is the system actor (the guarded retired-rows purge hands a
    doomed row's settled bill back this way, #284); scope gating applies to
    token-bearing callers only."""
    if token is not None and not security.can_write(token.scopes):
        raise LedgerError("unmatching requires write scope")
    match = get_match(session, match_id)
    if match.state != "confirmed":
        raise LedgerError(f"match is {match.state}, not confirmed")
    return release_settlement(
        session, match=match, actor_token_id=token.id if token is not None else None
    )


def release_settlement(
    session: Session,
    *,
    match: Match,
    actor_token_id: uuid.UUID | None,
    reason: str | None = None,
    keep_pin: bool = False,
) -> Transaction:
    """The body of ``unmatch``: reject a confirmed match and re-open its
    expected as a fresh clone in the same slot. Also what unlinking a
    merge-created transfer does to the settlement it carried (decision 65):
    a voided transfer settles nothing, so the instance is due again.
    ``actor_token_id=None`` is the system actor; scope is the caller's.
    ``keep_pin`` carries the instance's pin onto the clone: the feed taking a
    pending back (decision 69) re-opens the instance exactly as the user left
    it, so an override survives the next estimate refresh — and so does a
    recorded card statement (decision 80), copied onto the clone."""
    old_expected = _load_tx(session, match.expected_id)
    if not _real_legs(old_expected):
        raise LedgerError("original expected has no real leg")
    estimate_probe = _estimate_probe(session, old_expected)

    match.state = "rejected"
    match.decided_by = actor_token_id

    clone = Transaction(
        date=old_expected.date,
        payee_id=old_expected.payee_id,
        description=old_expected.description,
        status="expected",
        origin="system",
        schedule_id=old_expected.schedule_id,
        # The clone re-fills the same rule slot (#231): materialization must
        # not create a second instance for it.
        occurrence_date=old_expected.occurrence_date,
        schedule_pinned_at=old_expected.schedule_pinned_at if keep_pin else None,
        created_by=actor_token_id,
    )
    session.add(clone)
    session.flush()
    for p in old_expected.postings:
        session.add(
            Posting(
                transaction_id=clone.id,
                account_id=p.account_id,
                amount=p.amount,
                currency=p.currency,
                memo=p.memo,
            )
        )
    statement = session.get(InstanceStatement, old_expected.id) if keep_pin else None
    if statement is not None:
        session.add(
            InstanceStatement(
                transaction_id=clone.id,
                statement_balance=statement.statement_balance,
                statement_date=statement.statement_date,
                source=statement.source,
                recorded_by=statement.recorded_by,
                recorded_at=statement.recorded_at,
            )
        )
    session.flush()
    if clone.schedule_id is not None:
        from purser import schedules as schedules_service

        schedules_service.touch_instance(session, clone, pin=False)
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="match.unmatch",
        entity_type="match",
        entity_id=match.id,
        before={"expected_id": str(old_expected.id), "posted_id": str(match.posted_id)},
        after={
            "recreated_expected_id": str(clone.id),
            **({"reason": reason} if reason is not None else {}),
        },
    )
    _emit_price_change(session, estimate_probe, match=match)
    return clone


# ---------------------------------------------------------------------------
# Feed lifecycle: a live match whose pending bank row the feed voids (#273)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveMatch:
    """A live match on a bank row, captured BEFORE the feed voids that row:
    the estimate probe has to see the schedule's actuals as they were."""

    match: Match
    probe: tuple[Schedule, Decimal] | None


def live_match_on(session: Session, tx: Transaction) -> LiveMatch | None:
    """The proposed or confirmed match naming ``tx`` as its bank row, if any
    (``matches_posted_live_uniq`` allows at most one)."""
    match = session.execute(
        select(Match).where(Match.posted_id == tx.id, Match.state.in_(LIVE_MATCH_STATES))
    ).scalar_one_or_none()
    if match is None:
        return None
    probe = None
    if match.state == "confirmed":
        expected = session.get(Transaction, match.expected_id)
        if expected is not None:
            probe = _estimate_probe(session, expected)
    return LiveMatch(match=match, probe=probe)


def settle_voided_pending(
    session: Session,
    live: LiveMatch,
    *,
    successor: Transaction | None,
    actor_token_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> str:
    """The pending row ``live`` names was just voided — by the feed, or by
    hand through ``transition_status`` (DESIGN §4/§5, decision 69). A void
    row settles nothing, so the match must not stay on it; the settlement
    follows the bank line as it does into a merge (decision 65):

    - a **confirmed** match moves to the posted ``successor`` when no other
      confirmed match holds it (``matches.posted_id`` and the expected's
      ``superseded_by``, audited ``match.carry``; a proposal sitting on the
      successor yields and its expected is matched again at once);
    - a **proposal** is rescinded (``match.rescind``) — and, when the pending
      posted, its expected is matched again at once against what the bank
      really posted;
    - otherwise the confirmed match is released through
      ``release_settlement`` (``match.unmatch`` with the reason): the
      instance is due again in its slot, pin kept, and the next matching pass
      can settle it against whatever the bank really posted.

    The feed acts as the system actor with its own reasons. A manual void
    has no successor and passes the voiding token as ``actor_token_id`` and
    ``reason="pending_voided"`` (#343): the proposal is rescinded by that
    actor and not re-decided, the confirmed match released by that actor —
    pin kept, since the person decided about the bank row, not the instance.
    Returns the outcome (carried | rescinded | released)."""
    match = live.match
    if match.state == "proposed":
        _rescind(
            session,
            match,
            reason=reason or ("pending_posted" if successor else "pending_vanished"),
            actor_token_id=actor_token_id,
        )
        if successor is not None:
            _match_expected(session, _load_tx(session, match.expected_id))
        return "rescinded"
    if successor is not None and _carry_to_successor(session, match, successor):
        _emit_price_change(session, live.probe, match=match)
        return "carried"
    release_settlement(
        session,
        match=match,
        actor_token_id=actor_token_id,
        reason=reason or ("pending_vanished" if successor is None else "successor_matched"),
        keep_pin=True,
    )
    return "released"


def estimate_probes_on(session: Session, *txs: Transaction) -> list[LiveMatch]:
    """Before ``txs`` change: one probe per estimated schedule whose estimate
    reads one of them (#330), each with a confirmed match that leads there,
    for ``emit_estimate_changes`` to compare across the change.

    ``matched_actuals`` follows each confirmed match's bank row to its
    lineage head, so the matches that read a row are those whose bank row is
    that row or a row it superseded — a merge leaves a flat bill's match on
    the voided original (decision 65). Only ``superseded_by`` leads into a
    row that can still change status; a correction chain ends at a posted
    replacement. Each candidate's head is confirmed with the walk the
    estimate itself uses. Several rows are probed together when one change
    moves them all — a transfer absorb flips the transfer and supersedes the
    bank row into it (#343). Two matches of one schedule count it once, so a
    change emits once per schedule."""
    from purser import schedules as schedules_service

    ids = {tx.id for tx in txs}
    lineage = frontier = set(ids)
    for _ in range(schedules_service.CHAIN_MAX_HOPS):
        frontier = (
            set(
                session.execute(
                    select(Transaction.id).where(Transaction.superseded_by.in_(frontier))
                ).scalars()
            )
            - lineage
        )
        if not frontier:
            break
        lineage = lineage | frontier
    matches = list(
        session.execute(
            select(Match)
            .where(Match.state == "confirmed", Match.posted_id.in_(lineage))
            .order_by(Match.created_at, Match.id)
        ).scalars()
    )
    if not matches:
        return []
    heads = schedules_service.chain_heads(session, [m.posted_id for m in matches])
    probes: dict[uuid.UUID, LiveMatch] = {}
    for match in matches:
        if heads[match.posted_id] not in ids:
            continue
        expected = _load_tx(session, match.expected_id)
        if expected.schedule_id is None or expected.schedule_id in probes:
            continue
        probe = _estimate_probe(session, expected)
        if probe is not None:
            probes[expected.schedule_id] = LiveMatch(match=match, probe=probe)
    return list(probes.values())


def emit_estimate_changes(session: Session, probes: list[LiveMatch]) -> None:
    """After the change: ``recurring.price_changed`` for each probed schedule
    whose estimate moved (#330)."""
    for live in probes:
        _emit_price_change(session, live.probe, match=live.match)


def _carry_to_successor(session: Session, match: Match, successor: Transaction) -> bool:
    """Move a confirmed ``match`` from its voided pending to the pending's
    posted ``successor``, unless a confirmed match already holds that row
    (it settles another expected; this one is released instead). Variance is
    kept: a successor carries the pending's exact amount on the same account
    (the reconciler pairs on amount), so the perspective leg is unchanged."""
    holder = session.execute(
        select(Match).where(Match.posted_id == successor.id, Match.state.in_(LIVE_MATCH_STATES))
    ).scalar_one_or_none()
    if holder is not None and holder.state == "confirmed":
        return False
    displaced = None
    if holder is not None:  # a proposal yields to a decided match
        _rescind(session, holder, reason="successor_claimed")
        displaced = _load_tx(session, holder.expected_id)
    expected = _load_tx(session, match.expected_id)
    before = str(match.posted_id)
    match.posted_id = successor.id
    expected.superseded_by = successor.id
    session.flush()
    audit.record(
        session,
        actor_token_id=None,
        action="match.carry",
        entity_type="match",
        entity_id=match.id,
        before={"posted_id": before},
        after={
            "posted_id": str(successor.id),
            "expected_id": str(expected.id),
            "reason": "pending_posted",
        },
    )
    if displaced is not None:
        _match_expected(session, displaced)
    return True


def _rescind(
    session: Session, match: Match, *, reason: str, actor_token_id: uuid.UUID | None = None
) -> None:
    """Withdraw a proposal whose bank row moved on — the system actor, or the
    token that voided the row by hand (#343)."""
    match.state = "rejected"
    match.decided_by = actor_token_id
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="match.rescind",
        entity_type="match",
        entity_id=match.id,
        after={
            "expected_id": str(match.expected_id),
            "posted_id": str(match.posted_id),
            "rescinded_match_ids": [str(match.id)],
            "reason": reason,
        },
    )
    suggestions_service.on_proposal_closed(
        session,
        match,
        outcome=suggestions_service.STATUS_WITHDRAWN,
        actor_token_id=actor_token_id,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Unmatched reporting (both directions, with aging)
# ---------------------------------------------------------------------------


def list_unmatched(
    session: Session, *, direction: str, as_of: date_type | None = None
) -> list[dict[str, Any]]:
    today = as_of or date_type.today()
    if direction == "expected":
        rows = _expected_transactions(session)
        # The bank row each open proposal pairs an expected with (at most one
        # per expected, matches_expected_live_uniq): ``match`` on exactly that
        # pair confirms the proposal (decision 76), so the report names it.
        proposed = (
            {
                expected_id: (posted_id, match_id)
                for expected_id, posted_id, match_id in session.execute(
                    select(Match.expected_id, Match.posted_id, Match.id).where(
                        Match.state == "proposed",
                        Match.expected_id.in_([tx.id for tx in rows]),
                    )
                ).all()
            }
            if rows
            else {}
        )
        out = []
        for tx in rows:
            info = _match_input(tx)
            out.append(
                {
                    "transaction_id": tx.id,
                    "date": tx.date,
                    "amount": info.amount if info else None,
                    "payee": tx.payee.name if tx.payee else None,
                    "description": tx.description,
                    "aging_days": max(0, (today - tx.date).days),
                    "has_proposed_match": tx.id in proposed,
                    "proposed_posted_id": proposed[tx.id][0] if tx.id in proposed else None,
                    "proposal_id": proposed[tx.id][1] if tx.id in proposed else None,
                }
            )
        return sorted(out, key=lambda r: -r["aging_days"])
    if direction == "posted":
        matched = select(Match.posted_id).where(Match.state.in_(["proposed", "confirmed"]))
        rows = list(
            session.execute(
                select(Transaction)
                .options(
                    selectinload(Transaction.postings).selectinload(Posting.account),
                    selectinload(Transaction.payee),
                )
                .where(
                    Transaction.status.in_(MATCHABLE_STATUSES),
                    Transaction.origin == "import",
                    Transaction.superseded_by.is_(None),
                    Transaction.id.not_in(matched),
                )
            ).scalars()
        )
        out = []
        for tx in rows:
            info = _match_input(tx)
            out.append(
                {
                    "transaction_id": tx.id,
                    "date": tx.date,
                    "amount": info.amount if info else None,
                    "payee": tx.payee.name if tx.payee else None,
                    "description": tx.description,
                    "aging_days": max(0, (today - tx.date).days),
                    "has_proposed_match": False,
                    "proposed_posted_id": None,
                    "proposal_id": None,
                }
            )
        return sorted(out, key=lambda r: -r["aging_days"])
    raise LedgerError("direction must be expected | posted")


# ---------------------------------------------------------------------------
# Transfers — candidates only; merge requires explicit confirm
# ---------------------------------------------------------------------------


def _one_sided_import_rows(session: Session) -> list[Transaction]:
    """Live import-origin rows with a single real leg that no live match
    holds — the absorb pool, and the unheld part of ``transfer_pool``. A
    split row (decision 87) is not in it: absorbing or merging one would void
    its category legs, and split rows are read-only."""
    matched = select(Match.posted_id).where(Match.state.in_(["proposed", "confirmed"]))
    rows = session.execute(
        select(Transaction)
        .options(
            selectinload(Transaction.postings).selectinload(Posting.account),
            selectinload(Transaction.payee),
        )
        .where(
            Transaction.status.in_(MATCHABLE_STATUSES),
            Transaction.origin == "import",
            Transaction.superseded_by.is_(None),
            Transaction.id.not_in(matched),
        )
    ).scalars()
    # Rows on a CLOSED account leave the transfer pool: no new leg may post
    # there, so pairing/absorbing them could never complete (DESIGN §3.4).
    out: list[Transaction] = []
    for tx in rows:
        leg = _real_leg(tx)
        if leg is None or leg.account.closed_at is not None or _is_split(tx):
            continue
        out.append(tx)
    return out


def _is_split(tx: Transaction) -> bool:
    return (
        core.split_line_count(
            [core.Leg(p.account_id, Decimal(p.amount), p.account.is_virtual) for p in tx.postings]
        )
        > 0
    )


@dataclass(frozen=True)
class TransferHold:
    """A one-sided bank row whose live match (proposed or confirmed) is to an
    expected TRANSFER (issue #270, decision 65). The row is still one side of
    that transfer, so it stays in the pairing pool — held for the expected's
    other account, the only side it may pair with."""

    row: Transaction
    match: Match
    expected: Transaction
    pair_account_id: uuid.UUID


def _transfer_sides(tx: Transaction) -> tuple[core.TransferShape, Posting, Posting] | None:
    """The transfer shape ``tx``'s postings form (decision 62) with its
    (outgoing, incoming) real legs — a split's cash and principal legs — or
    None for any other row."""
    if any(p.account is None for p in tx.postings):
        return None
    shape = core.transfer_shape(
        [core.Leg(p.account_id, Decimal(p.amount), p.account.is_virtual) for p in tx.postings]
    )
    if shape is None:
        return None
    outgoing, incoming = sorted(_real_legs(tx), key=lambda p: Decimal(p.amount))
    return shape, outgoing, incoming


def _hold_target(row: Transaction, expected: Transaction) -> uuid.UUID | None:
    """The account ``row`` is held for when its live match is to ``expected``:
    the other side of a plain expected transfer the row is one side of (same
    account, same direction). None for a bill, or a row that is neither side."""
    leg = _real_leg(row)
    sides = _transfer_sides(expected)
    if leg is None or sides is None or sides[0] is not core.TransferShape.PLAIN:
        return None
    _, outgoing, incoming = sides
    amount = Decimal(leg.amount)
    if leg.account_id == outgoing.account_id and amount < 0:
        return incoming.account_id
    if leg.account_id == incoming.account_id and amount > 0:
        return outgoing.account_id
    return None


def _transfer_holds(
    session: Session, *, account_id: uuid.UUID | None = None
) -> dict[uuid.UUID, TransferHold]:
    """Live one-sided bank rows that a live match holds for an expected
    transfer, by row id (optionally only rows on ``account_id``). The SQL
    keeps it to matches whose expected has no category leg — bills never
    load — and the shape check runs on what is left."""
    has_category_leg = (
        select(Posting.id)
        .join(Account, Account.id == Posting.account_id)
        .where(Posting.transaction_id == Match.expected_id, Account.is_virtual.is_(True))
        .exists()
    )
    held = (
        select(Match.id)
        .join(Transaction, Transaction.id == Match.posted_id)
        .where(
            Match.state.in_(LIVE_MATCH_STATES),
            Transaction.status.in_(MATCHABLE_STATUSES),
            Transaction.origin == "import",
            Transaction.superseded_by.is_(None),
            ~has_category_leg,
        )
    )
    if account_id is not None:
        held = held.where(
            Match.posted_id.in_(
                select(Posting.transaction_id).where(Posting.account_id == account_id)
            )
        )
    matches = list(session.execute(select(Match).where(Match.id.in_(held))).scalars())
    if not matches:
        return {}
    wanted = (
        select(Match.posted_id)
        .where(Match.id.in_(held))
        .union(select(Match.expected_id).where(Match.id.in_(held)))
    )
    loaded = {
        t.id: t
        for t in session.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.postings).selectinload(Posting.account),
                selectinload(Transaction.payee),
            )
            .where(Transaction.id.in_(wanted))
        ).scalars()
    }
    holds: dict[uuid.UUID, TransferHold] = {}
    for match in matches:
        row, expected = loaded[match.posted_id], loaded[match.expected_id]
        leg = _real_leg(row)
        if leg is None or leg.account.closed_at is not None:
            continue  # a closed account leaves the transfer pool (DESIGN §3.4)
        target = _hold_target(row, expected)
        if target is not None:
            holds[row.id] = TransferHold(
                row=row, match=match, expected=expected, pair_account_id=target
            )
    return holds


def transfer_pool(session: Session) -> tuple[list[Transaction], dict[uuid.UUID, TransferHold]]:
    """The rows transfer pairing draws from (DESIGN §5, decision 65):
    unmatched one-sided import rows, plus the ones a live match holds for an
    expected transfer. A matched leg used to leave the pool, so when the two
    legs of a scheduled transfer posted on different days the matcher took
    the first and its counterpart never linked. Shared by the auto-linker's
    plan (``transfers.plan_auto_links`` — so a transfer_link rule's projected
    count too) and the candidate listing, so they pair from one pool."""
    holds = _transfer_holds(session)
    return _one_sided_import_rows(session) + [h.row for h in holds.values()], holds


def detect_transfer_pairs_for(
    session: Session,
    rows: list[Transaction],
    *,
    holds: dict[uuid.UUID, TransferHold] | None = None,
    window: core_matching.PairWindow | None = None,
) -> tuple[list[core_matching.TransferCandidate], dict[uuid.UUID, Transaction]]:
    """Different-account opposite-amount pairs among ``rows`` + an id lookup.
    A row in ``holds`` pairs only with its expected's other side (decision
    65); ``window`` is ``core.matching.detect_transfer_pairs``'s per-pair
    window (decision 66)."""
    holds = holds or {}
    inputs: list[core_matching.MatchInput] = []
    account_of: dict[uuid.UUID, uuid.UUID] = {}
    tx_of: dict[uuid.UUID, Transaction] = {}
    for tx in rows:
        info = _match_input(tx)
        leg = _real_leg(tx)
        if info is None or leg is None:
            continue
        hold = holds.get(tx.id)
        if hold is not None:
            info = replace(info, pair_account_id=hold.pair_account_id)
        inputs.append(info)
        account_of[tx.id] = leg.account_id
        tx_of[tx.id] = tx
    # account_id rides inside MatchInput now (issue #173) so the pure
    # function refuses same-account pairs during pairing; this filter stays
    # as belt-and-suspenders.
    pairs = [
        pair
        for pair in core_matching.detect_transfer_pairs(inputs, window=window)
        if account_of[pair.outgoing_id] != account_of[pair.incoming_id]
    ]
    return pairs, tx_of


def list_transfer_candidates(session: Session) -> list[dict[str, Any]]:
    rows, holds = transfer_pool(session)
    pairs, tx_of = detect_transfer_pairs_for(session, rows, holds=holds)
    out = []
    for pair in pairs:
        out.append(
            {
                "outgoing_id": pair.outgoing_id,
                "incoming_id": pair.incoming_id,
                "amount": pair.amount,
                "date_gap_days": pair.date_gap_days,
                "outgoing_description": tx_of[pair.outgoing_id].description,
                "incoming_description": tx_of[pair.incoming_id].description,
            }
        )
        _emit_transfer_candidate_once(session, pair)
    return out


def _emit_transfer_candidate_once(session: Session, pair: core_matching.TransferCandidate) -> None:
    from purser.models import Event

    pair_key = (
        f"{min(pair.outgoing_id, pair.incoming_id)}:{max(pair.outgoing_id, pair.incoming_id)}"
    )
    exists = session.execute(
        select(Event.id).where(
            Event.type == "transfer.candidate",
            Event.dedup_key == pair_key,
        )
    ).first()
    if exists is None:
        events.emit(
            session,
            type_="transfer.candidate",
            entity_type="transaction",
            entity_id=pair.outgoing_id,
            payload={
                "pair_key": pair_key,
                "outgoing_id": str(pair.outgoing_id),
                "incoming_id": str(pair.incoming_id),
                "amount": str(pair.amount),
            },
            dedup_key=pair_key,
        )


def _merge_transfer_pair(
    session: Session,
    *,
    outgoing: Transaction,
    incoming: Transaction,
    token: ApiToken | None,
    origin: core.Origin | None = None,
    action: str = "transfer.confirm",
    event_type: str = "transfer.confirmed",
    remainder_category_ref: str | None = None,
    signal: str | None = None,
) -> Transaction:
    """Merge two one-sided rows into ONE transfer transaction; the originals
    are voided with lineage links. ``token=None`` is the system auto-linker;
    human/agent confirms pass their token (scope checked by the caller).

    ``remainder_category_ref`` makes the merge a split loan payment (DESIGN
    §3.3, decision 62): the two bank legs keep their exact, unequal amounts
    and the difference books to that interest/fee category. It is
    explicit-only — a token-less (system) caller can never pass it.

    ``signal`` is the auto-linker's reason (``name``, ``rule:<id>``,
    ``schedule:<id>``), recorded on the audit row and the event. A live match
    on either original follows the bank line into the merge (decision 65,
    ``_carry_settlements``)."""
    for tx in (outgoing, incoming):
        if tx.status not in MATCHABLE_STATUSES:
            # Replays land here: a confirmed merge already voided the originals.
            raise LedgerError(f"transaction {tx.id} is {tx.status}, not matchable")
    out_leg, in_leg = _real_leg(outgoing), _real_leg(incoming)
    if out_leg is None or in_leg is None:
        raise LedgerError("both transactions need a single real-account leg")
    if out_leg.account_id == in_leg.account_id:
        raise LedgerError("transfer legs must be on different accounts")
    out_amount, in_amount = Decimal(out_leg.amount), Decimal(in_leg.amount)
    if out_amount >= 0:
        raise LedgerError("outgoing leg must be negative")
    split = remainder_category_ref is not None
    if split and token is None:
        raise LedgerError("a split loan payment is confirmed explicitly, never automatically")
    if split and out_amount == -in_amount:
        raise LedgerError(
            f"the legs already cancel ({out_amount} / {in_amount}): confirm without "
            "remainder_category for an ordinary transfer"
        )
    if not split and out_amount != -in_amount:
        hint = (
            f"; if the {-(out_amount + in_amount)} difference is loan interest or fees, "
            "confirm with remainder_category (an expense category) to book it"
            if 0 < in_amount < -out_amount
            else ""
        )
        raise LedgerError(f"transfer legs must have opposite amounts{hint}")

    from_account = session.get(Account, out_leg.account_id)
    to_account = session.get(Account, in_leg.account_id)
    assert from_account is not None and to_account is not None
    status = (
        core.Status.POSTED
        if outgoing.status == "posted" and incoming.status == "posted"
        else core.Status.PENDING
    )
    description = f"Transfer: {outgoing.description or ''} / {incoming.description or ''}".strip()
    if remainder_category_ref is not None:
        merged = ledger_service.create_split_transfer(
            session,
            outgoing_account_ref=str(from_account.id),
            outgoing_amount=out_amount,
            incoming_account_ref=str(to_account.id),
            incoming_amount=in_amount,
            remainder_category_ref=remainder_category_ref,
            date=min(outgoing.date, incoming.date),
            description=description,
            requested_status=status,
            token=token,
            origin=origin,
        )
    else:
        merged = ledger_service.create_transfer(
            session,
            from_account_ref=str(from_account.id),
            to_account_ref=str(to_account.id),
            amount=abs(out_amount),
            date=min(outgoing.date, incoming.date),
            description=description,
            requested_status=status,
            token=token,
            origin=origin,
        )
    # Split-only keys, so the long-standing payload shape stays exact for
    # every ordinary transfer; the signal likewise rides only on auto-links.
    split_fields = _split_fields(merged)
    signal_field = {"signal": signal} if signal is not None else {}
    # Prior statuses ride on the event so unlink can resurrect each side
    # exactly as it was.
    prior_statuses = {
        "outgoing_status": outgoing.status,
        "incoming_status": incoming.status,
    }
    # Read before the originals are voided: what the bank lines settle.
    held = list(
        session.execute(
            select(Match)
            .where(
                Match.posted_id.in_([outgoing.id, incoming.id]),
                Match.state.in_(LIVE_MATCH_STATES),
            )
            .order_by(Match.created_at, Match.id)
        ).scalars()
    )
    for original in (outgoing, incoming):
        original.status = "void"
        original.superseded_by = merged.id
    session.flush()
    # The funnel marked the merged transfer at min(dates); mark the voids
    # explicitly too so this stays correct if the ordering ever changes.
    balances.mark_dirty(session, min(outgoing.date, incoming.date))
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action=action,
        entity_type="transaction",
        entity_id=merged.id,
        after={
            "outgoing_id": str(outgoing.id),
            "incoming_id": str(incoming.id),
            "amount": str(abs(out_amount)),
            **({"incoming_amount": str(in_amount), **split_fields} if split_fields else {}),
            **signal_field,
        },
    )
    events.emit(
        session,
        type_=event_type,
        entity_type="transaction",
        entity_id=merged.id,
        payload={
            "outgoing_id": str(outgoing.id),
            "incoming_id": str(incoming.id),
            **prior_statuses,
            "pair_key": _pair_key(outgoing.id, incoming.id),
            **split_fields,
            **signal_field,
        },
        dedup_key=_pair_key(outgoing.id, incoming.id),
    )
    _carry_settlements(
        session, held=held, merged=merged, actor_token_id=token.id if token else None
    )
    return merged


def _carry_settlements(
    session: Session,
    *,
    held: list[Match],
    merged: Transaction,
    actor_token_id: uuid.UUID | None,
) -> None:
    """A live match on a merged original follows its bank line into the
    merge (decision 65), so an expected is settled exactly once and never by
    a dead row:

    - **confirmed**, and the expected is a plain transfer between the merge's
      two accounts in the same direction: the match moves to the merge
      (``posted_id`` and the expected's ``superseded_by``) — the settlement
      stands, and the merge is matched, so no second expected can claim it.
      Any other confirmed match stays where it was decided.
    - **proposed**: rescinded (it names a row that no longer exists) and the
      expected is matched again at once through the ordinary matcher, which
      for an instance the merge covers is an auto match on the merge.
    """
    if not held:
        return
    sides = _transfer_sides(merged)
    assert sides is not None  # _merge_transfer_pair just wrote a transfer shape
    _, out_leg, in_leg = sides
    rematch: list[Transaction] = []
    carried = False
    for match in held:
        expected = _load_tx(session, match.expected_id)
        if match.state == "proposed":
            match.state = "rejected"
            match.decided_by = actor_token_id
            session.flush()
            audit.record(
                session,
                actor_token_id=actor_token_id,
                action="match.rescind",
                entity_type="match",
                entity_id=match.id,
                after={
                    "expected_id": str(expected.id),
                    "posted_id": str(match.posted_id),
                    "rescinded_match_ids": [str(match.id)],
                    "reason": "transfer.merge",
                    "merged_id": str(merged.id),
                },
            )
            suggestions_service.on_proposal_closed(
                session,
                match,
                outcome=suggestions_service.STATUS_WITHDRAWN,
                actor_token_id=actor_token_id,
                reason="transfer.merge",
            )
            rematch.append(expected)
            continue
        covered = _transfer_sides(expected)
        if (
            carried
            or covered is None
            or covered[0] is not core.TransferShape.PLAIN
            or (covered[1].account_id, covered[2].account_id)
            != (out_leg.account_id, in_leg.account_id)
        ):
            continue
        before = str(match.posted_id)
        match.posted_id = merged.id
        expected.superseded_by = merged.id
        session.flush()
        carried = True
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="match.carry",
            entity_type="match",
            entity_id=match.id,
            before={"posted_id": before},
            after={"posted_id": str(merged.id), "expected_id": str(expected.id)},
        )
    for expected in rematch:
        _match_expected(session, expected)


def _pair_key(a: uuid.UUID, b: uuid.UUID) -> str:
    return f"{min(a, b)}:{max(a, b)}"


def confirm_transfer(
    session: Session,
    *,
    outgoing_id: uuid.UUID,
    incoming_id: uuid.UUID,
    token: ApiToken,
    remainder_category_ref: str | None = None,
) -> Transaction:
    """Explicit human/agent confirm of a transfer merge (write scope); see
    :func:`confirm_transfer_and_propose`, which also returns the suggestion
    the confirm proposed."""
    merged, _ = confirm_transfer_and_propose(
        session,
        outgoing_id=outgoing_id,
        incoming_id=incoming_id,
        token=token,
        remainder_category_ref=remainder_category_ref,
    )
    return merged


def confirm_transfer_and_propose(
    session: Session,
    *,
    outgoing_id: uuid.UUID,
    incoming_id: uuid.UUID,
    token: ApiToken,
    remainder_category_ref: str | None = None,
) -> tuple[Transaction, Suggestion | None]:
    """Explicit human/agent confirm of a transfer merge (write scope), plus
    the transfer_link suggestion it proposed (decision 67), if any.

    With ``remainder_category_ref`` the pair is a split loan payment (DESIGN
    §3.3, decision 62): the unequal bank legs stay exact and the difference
    books to that interest/fee category. A blank ref means "none" — clients
    (and agents) send ``""`` for an omitted optional string.

    An ordinary confirm proposes a payee-scoped ``transfer_link`` rule in the
    suggestions inbox and nothing more — no hidden memory, nothing that links
    by itself (#271). A split proposes nothing: every automatic path needs
    exactly opposite amounts, so a rule could never link its shape."""
    if not security.can_write(token.scopes):
        raise LedgerError("confirming a transfer requires write scope")
    outgoing = _load_tx(session, outgoing_id)
    incoming = _load_tx(session, incoming_id)
    remainder_category = (remainder_category_ref or "").strip() or None
    merged = _merge_transfer_pair(
        session,
        outgoing=outgoing,
        incoming=incoming,
        token=token,
        remainder_category_ref=remainder_category,
    )
    # Two bank rows becoming one transfer is precisely the class of automation
    # a reviewer is asked to sanity-check — but a HUMAN just did it, so the
    # merged row leaves the unreviewed set (#175).
    review_service.mark_if_in_scope(
        session, tx=merged, token=token, via=review_service.VIA_CONFIRM_TRANSFER
    )
    suggestion: Suggestion | None = None
    if remainder_category is None:
        from purser import suggestions as suggestions_service

        suggestion = suggestions_service.propose_transfer_link(
            session, outgoing=outgoing, incoming=incoming, merged=merged, token=token
        )
    return merged, suggestion


def _split_fields(tx: Transaction) -> dict[str, str]:
    """``remainder`` / ``remainder_category`` of a split loan payment (decision
    62); empty for every other row, so ordinary payloads keep their shape."""
    from purser import transfers

    leg = transfers.split_remainder(tx)
    if leg is None:
        return {}
    return {"remainder": str(Decimal(leg.amount)), "remainder_category": leg.account.path}


def transfer_confirmation(
    merged: Transaction, suggestion: Suggestion | None = None
) -> dict[str, str]:
    """What a confirm returns, on REST and MCP alike: the merged id, plus —
    for a split loan payment only — the remainder and the category it booked
    to, so the caller sees exactly what was recorded without a second read;
    plus ``suggestion_id`` when the confirm proposed (or refreshed) a
    transfer_link suggestion in the inbox (decision 67)."""
    out = {"merged_transaction_id": str(merged.id), **_split_fields(merged)}
    if suggestion is not None:
        out["suggestion_id"] = str(suggestion.id)
    return out


def run_matching_job() -> None:
    """Scheduled daily matching pass (worker/serve job).

    Runs right after the morning feed sync so freshly imported rows are
    matched against expecteds before late-bill aging and forecasts read them.
    Never raises: failures are rolled back and logged for the scheduler.
    """
    from purser.db import get_sessionmaker
    from purser.logging import get_logger

    log = get_logger(__name__)
    with get_sessionmaker()() as session:
        try:
            stats = run_matching(session, actor_token_id=None)
            # Sweep for transfers whose counterpart arrived through another
            # source (manual entry, other connection) since the last import.
            from purser import transfers

            stats.update(transfers.auto_link_pass(session))
            session.commit()
            log.info("scheduled matching complete", **stats)
        except Exception:
            session.rollback()
            log.exception("scheduled matching failed")
