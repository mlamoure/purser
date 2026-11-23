"""Suggestions inbox tools (issue #233, DESIGN decisions 57, 67, 86 and 102).

`list_suggestions` (read) and `decide_suggestion` (write) are the agent's
rendering of the same rows the Dashboard cards show. Nothing here creates a
suggestion: detection, the liability-added hook, `create_schedule` with
`propose=true`, a manual `confirm_transfer`, the matcher and the
transfer-candidate pass after an import do that.
"""

import uuid
from datetime import date as date_type
from typing import Any

from sqlalchemy.orm import Session

from purser.mcp._registry import _amount, _principal, _run, _token_row, audited_tool
from purser.privacy import Emits

# What a serialized suggestion carries that minimal disclosure governs
# (suggestions.serialize; a match draft quotes both rows and the variance,
# a transfer_pair draft both bank texts and the amount moved).
SUGGESTION_TEXT = (
    "items[].draft.name",
    "items[].draft.label",
    "items[].draft.outgoing_description",
    "items[].draft.incoming_description",
    "items[].evidence_summary.note",
    "items[].draft.lines[].memo",
    "items[].draft.match_text",
)
SUGGESTION_AMOUNTS = (
    "items[].draft.amount",
    "items[].draft.expected_amount",
    "items[].draft.posted_amount",
    "items[].draft.variance",
    "items[].draft.template_amount",
    "items[].draft.lines[].amount",
    "items[].evidence_summary.amount_range",
    "items[].evidence_summary.latest_amount",
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
    offset: int = 0,
) -> dict[str, Any]:
    """What Purser thinks you should decide. kind='schedule': detected
    recurring bills and transfers, agent-proposed schedules, payments for a
    newly added card. kind='transfer_link': a transfer_link rule a manual
    confirm_transfer proposed for that account pair, scoped to the payee and
    its aliases (evidence = the confirmed pairs). kind='match': an open match
    proposal — an expected bill paired with a bank row (draft = both rows'
    dates and amounts on `account`, and the variance; confidence = the match
    score). kind='transfer_pair': two bank rows of exactly opposite amounts
    on two accounts, close in date, that nothing links automatically — is it
    one transfer? (draft = both rows, from_account/to_account, amount, the
    dates and both bank texts). kind='split': "split like last time?" — a new
    bank row from a payee whose last split a person or agent made (draft =
    the row, the template split, and the lines to apply: the template's,
    with the amount difference on its largest category line, and an account
    line's one exact backing row when there is exactly one). kind='trade_rule':
    a rule proposed because a row was marked a securities trade — it would
    mark rows like it on that brokerage account the same way (draft =
    account, match_field, match_mode, match_text, direction). Omit kind for
    all. Each row carries a
    one-line headline naming accounts as people know them (the draft keeps
    their paths, which other tools take), the typed draft accept would apply
    unchanged, an
    evidence summary (count, first/last, amount range, latest amount and
    possible price change, cadence, and the
    proposing agent's note if it left one), and a confidence (null for
    transfer links, transfer pairs and agent proposals, which nothing
    scored). Snoozed rows are hidden until their date. `open` counts every
    open row and `open_by_kind` each kind's, so `limit` never hides how
    many wait; `offset` skips the rows of earlier pages to reach the rest.
    Decide with decide_suggestion; GET /suggestions/{id} has the
    full evidence."""

    def op(session: Session) -> dict[str, Any]:
        from purser import suggestions as service

        rows = service.list_suggestions(
            session,
            kind=kind,
            account_ref=account,
            include_snoozed=include_snoozed,
            limit=limit,
            offset=offset,
        )
        names = service.AccountNames(session)
        return {
            "open": service.open_count(session),
            "open_by_kind": service.open_counts(session),
            "returned": len(rows),
            "items": [service.serialize(session, r, names=names) for r in rows],
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
    decimal string, window_days 0 to 14, effective_from YYYY-MM-DD;
    trade_rule: match_field, match_mode, match_text, direction). A trade_rule
    suggestion becomes a DRAFT categorize rule marking rows as a securities
    trade (rule.projected_matches is how many live rows it selects); it
    marks nothing until activated, and history only through apply-history
    (dry run first). A match
    suggestion: accept confirms the proposal (match{id,state,variance} in
    the result) and reject rejects it for good — the matcher never proposes
    that pair again; it has nothing to edit, and a proposal already gone
    withdraws the suggestion with an error. A transfer_pair suggestion:
    accept merges the two rows into one transfer, as confirm_transfer does
    (transfer{merged_transaction_id, suggestion_id} — the transfer_link rule
    the confirm proposed, when it proposed one), and reject means "not a
    transfer": nothing ever links that pair automatically; it has nothing to
    edit, and a pair whose rows moved on withdraws it with an error. A split
    suggestion: accept splits the row with the draft's lines (as
    split_transaction does; split{transaction_id} in the result) and
    edit_then_accept with edits={"lines": [...]} (same line shape as
    split_transaction) applies changed lines; reject (reject_scope=
    counterparty: never for this payee on this account). reject
    is durable — reject_scope=counterparty means never suggest this
    payee/counterparty on this account again (for a transfer_link: this
    account pair, any payee; for a trade_rule: any trade rule for this
    account; a match or transfer_pair rejects exactly);
    snooze hides it until snooze_until (YYYY-MM-DD, default 30 days).
    Nothing auto-accepts."""

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
            }
            if created.type == rules_service.TYPE_TRANSFER_LINK:
                out["rule"]["projected_links"] = rules_service.project_links(session, created)
                out["rule"]["note"] = (
                    "draft: a human activates it on the Rules page, confirming projected_links"
                )
            else:
                out["rule"]["projected_matches"] = rules_service.project_matches(session, created)
                out["rule"]["note"] = (
                    "draft: marks nothing until a human activates it on the Rules page; "
                    "apply-history then marks past rows, dry run first"
                )
        elif isinstance(created, Match):
            out["match"] = {
                "id": str(created.id),
                "state": created.state,
                "variance": _amount(created.variance),
            }
        if row.kind == service.KIND_SPLIT and row.result_entity_id is not None:
            out["split"] = {"transaction_id": str(row.result_entity_id)}
        if isinstance(created, service.TransferPairConfirmed):
            from purser import matching

            out["transfer"] = matching.transfer_confirmation(created.merged, created.proposed)
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
        names = service.AccountNames(session)
        return {
            "created": len(created),
            "refreshed": len(touched) - len(created),
            "open": service.open_count(session),
            "items": [service.serialize(session, r, names=names) for r in touched[:10]],
        }

    return await _run(op)
