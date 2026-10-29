"""Suggestions inbox tools (issue #233, DESIGN decisions 57, 67 and 86).

`list_suggestions` (read) and `decide_suggestion` (write) are the agent's
rendering of the same rows the Dashboard cards show. Nothing here creates a
suggestion: detection, the liability-added hook, `create_schedule` with
`propose=true`, a manual `confirm_transfer` and the matcher do that.
"""

import uuid
from datetime import date as date_type
from typing import Any

from sqlalchemy.orm import Session

from purser.mcp._registry import _amount, _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import Emits

# What a serialized suggestion carries that minimal disclosure governs
# (suggestions.serialize; a match draft quotes both rows and the variance).
SUGGESTION_TEXT = (
    "items[].draft.name",
    "items[].draft.label",
    "items[].evidence_summary.note",
)
SUGGESTION_AMOUNTS = (
    "items[].draft.amount",
    "items[].draft.expected_amount",
    "items[].draft.posted_amount",
    "items[].draft.variance",
    "items[].evidence_summary.amount_range",
)


@audited_tool(
    kind="read",
    entity="suggestion",
    scope="read",
    emits=Emits(
        payee=True,
        narrative=True,
        rule_text=True,
        description=SUGGESTION_TEXT,
        amount=SUGGESTION_AMOUNTS,
    ),
)
async def list_suggestions(
    kind: str | None = None,
    account: str | None = None,
    include_snoozed: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """What Purser thinks you should decide. kind='schedule': detected
    recurring bills and transfers, agent-proposed schedules, payments for a
    newly added card. kind='transfer_link': a transfer_link rule a manual
    confirm_transfer proposed for that account pair, scoped to the payee and
    its aliases (evidence = the confirmed pairs). kind='match': an open match
    proposal — an expected bill paired with a bank row (draft = both rows'
    dates and amounts on `account`, and the variance; confidence = the match
    score). Omit kind for all. Each row carries a one-line headline, the
    typed draft accept would apply unchanged, an evidence summary (count,
    first/last, amount range, cadence, and the proposing agent's note if it
    left one), and a confidence (null for transfer links and agent
    proposals, which nothing scored). Snoozed rows are hidden until their
    date. Decide with decide_suggestion; GET /suggestions/{id} has the full
    evidence."""

    def op(session: Session) -> dict[str, Any]:
        from purser import suggestions as service

        rows = service.list_suggestions(
            session,
            kind=kind,
            account_ref=account,
            include_snoozed=include_snoozed,
            limit=limit,
        )
        return {
            "open": service.open_count(session),
            "returned": len(rows),
            "items": [service.serialize(session, r) for r in rows],
        }

    return await _run(op)


@audited_tool(
    kind="write",
    entity="suggestion",
    scope="write",
    emits=Emits(payee=True, narrative=True, rule_text=True, amount=("match.variance",)),
)
async def decide_suggestion(
    suggestion_id: str,
    decision: str,
    edits: dict[str, Any] | None = None,
    note: str | None = None,
    reject_scope: str = "exact",
    snooze_until: str | None = None,
) -> dict[str, Any]:
    """Decide a suggestion (write scope). accept applies the draft exactly
    as shown: a schedule suggestion becomes an active, materialized schedule;
    a transfer_link suggestion becomes a DRAFT rule (rule.projected_links is
    how many past pairs it would link) that links nothing until a human
    activates it on the Rules page, confirming that count. edit_then_accept
    applies it with `edits` (schedule draft fields: name, rrule, dtstart,
    amount, amount_type, estimate_basis, payee, category,
    counterparty_account, autopost_policy; transfer_link: amount_exact as a
    decimal string, window_days 0 to 14, effective_from YYYY-MM-DD). A match
    suggestion: accept confirms the proposal (match{id,state,variance} in
    the result) and reject rejects it for good — the matcher never proposes
    that pair again; it has nothing to edit, and a proposal already gone
    withdraws the suggestion with an error. reject is durable —
    reject_scope=counterparty means never suggest this payee/counterparty on
    this account again (for a transfer_link: this account pair, any payee;
    a match rejects exactly); snooze hides it until snooze_until
    (YYYY-MM-DD, default 30 days). Nothing auto-accepts."""

    def op(session: Session) -> dict[str, Any]:
        from purser import rules as rules_service
        from purser import suggestions as service
        from purser.api.rules import _Accounts, _render_action, _render_predicate
        from purser.models import Match, Rule, Schedule

        principal = _principal()
        try:
            row, created = service.decide(
                session,
                suggestion_id=uuid.UUID(suggestion_id),
                decision=decision,
                token=_token_row(session, principal),
                edits=edits,
                note=note,
                reject_scope=reject_scope,
                snooze_until=date_type.fromisoformat(snooze_until) if snooze_until else None,
            )
        except service.SuggestionWithdrawn:
            session.commit()  # the withdrawal stands; the card leaves the inbox
            raise
        out: dict[str, Any] = {
            "id": str(row.id),
            "kind": row.kind,
            "status": row.status,
            "decision": decision,
            "snooze_until": row.snooze_until.isoformat() if row.snooze_until else None,
        }
        if isinstance(created, Schedule):
            out["schedule"] = {
                "id": str(created.id),
                "status": created.status,
                "next_due": created.next_due.isoformat() if created.next_due else None,
            }
        elif isinstance(created, Rule):
            accounts = _Accounts(session)
            out["rule"] = {
                "id": str(created.id),
                "state": created.state,
                "when": _render_predicate(created, accounts),
                "then": _render_action(created, accounts),
                "projected_links": rules_service.project_links(session, created),
                "note": "draft: a human activates it on the Rules page, confirming projected_links",
            }
        elif isinstance(created, Match):
            out["match"] = {
                "id": str(created.id),
                "state": created.state,
                "variance": _amount(created.variance),
            }
        return out

    return await _run(op)


@audited_tool(
    kind="write",
    entity="suggestion",
    scope="write",
    emits=Emits(
        payee=True,
        narrative=True,
        rule_text=True,
        description=SUGGESTION_TEXT,
        amount=SUGGESTION_AMOUNTS,
    ),
)
async def detect_schedules(account: str | None = None) -> dict[str, Any]:
    """Scan history for recurring patterns now (the nightly detector does
    this at 07:05 UTC) and land them in the suggestions inbox: one-sided
    payees by cadence, and transfers — card/loan payments, contributions — by
    counterparty account. Nothing activates; rejected patterns and pairs a
    schedule already covers are skipped. account narrows the scan."""

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as schedules_service
        from purser import suggestions as service

        principal = _principal()
        before = {
            r.id
            for r in service.list_suggestions(
                session, status="all", kind=service.KIND_SCHEDULE, limit=25
            )
        }
        touched = schedules_service.detect_schedules(
            session, actor_token_id=_token_row(session, principal).id, account_ref=account
        )
        created = [r for r in touched if r.id not in before]
        return {
            "created": len(created),
            "refreshed": len(touched) - len(created),
            "open": service.open_count(session),
            "items": [service.serialize(session, r) for r in touched[:10]],
        }

    return await _run(op)
