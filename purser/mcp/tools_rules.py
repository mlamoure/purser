"""Deterministic rule tools (issue #174, DESIGN decision 46).

Registered on import by ``purser.mcp.server``, like the other tool groups.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import Emits

_LIST_CAP = 50
_SAMPLE_CAP = 10


@audited_tool(kind="read", entity="rule", scope="read", emits=Emits(payee=True, rule_text=True))
async def list_rules(state: str | None = None, type: str | None = None) -> dict[str, Any]:
    """Deterministic rules that run on new transactions: categorize, rename,
    transfer links. Returned in EVALUATION order, so which rule wins is
    visible before it fires. state='draft' is the review list for rules an
    agent proposed or a transfer-link suggestion produced. A transfer_link
    rule also carries window_days: how many days apart its pair's two legs
    may post and still link; and payee_scope when it links only pairs where
    a leg carries that payee or an alias of it. applied counts the times a
    rule fired: once per row a categorize or rename rule shaped (a run over
    history counts once), once per link (merge or absorb) a transfer_link
    rule made. Previews and the projected link count at activation never
    count, and an unlink does not take a count back."""

    def op(session: Session) -> dict[str, Any]:
        from purser.api.rules import (
            _Accounts,
            _render_action,
            _render_predicate,
            payee_scope_of,
            window_of,
        )
        from purser.models import Rule

        stmt = select(Rule).order_by(Rule.priority, Rule.created_at, Rule.id)
        if state:
            stmt = stmt.where(Rule.state == state)
        if type:
            stmt = stmt.where(Rule.type == type)
        rows = list(session.execute(stmt).scalars())
        accounts = _Accounts(session)
        return {
            "total": len(rows),
            "rules": [
                {
                    "id": str(r.id),
                    "type": r.type,
                    "state": r.state,
                    "priority": r.priority,
                    "when": _render_predicate(r, accounts),
                    "then": _render_action(r, accounts),
                    "applied": r.applied_count,
                    # Only a transfer_link rule has a window (decision 66).
                    **({"window_days": window} if (window := window_of(r)) is not None else {}),
                    # ...and maybe a payee scope (decision 67).
                    **(
                        {"payee_scope": scope}
                        if (scope := payee_scope_of(r, accounts)) is not None
                        else {}
                    ),
                }
                for r in rows[:_LIST_CAP]
            ],
            "truncated": max(0, len(rows) - _LIST_CAP),
        }

    return await _run(op)


@audited_tool(
    kind="read",
    entity="rule",
    scope="read",
    emits=Emits(
        payee=True,
        rule_text=True,
        description=("sample[].description",),
        amount=("sample[].amount",),
    ),
)
async def preview_rule(
    match_field: str | None = None,
    match_mode: str | None = None,
    match_text: str | None = None,
    account: str | None = None,
    amount_exact: str | None = None,
    direction: str | None = None,
    pair_accounts: list[str] | None = None,
    window_days: int | None = None,
    effective_from: str | None = None,
) -> dict[str, Any]:
    """Dry-run a rule over history: how many rows it would touch, with a
    small sample. Use this before proposing a rule — the count is what tells
    you whether the predicate is too broad. With pair_accounts (two account
    references) it previews a transfer_link rule instead: links is exactly
    the number of historical pairs activating that rule would link, which is
    the count its activation asks the user to confirm. A transfer_link takes
    no text predicate; amount_exact, effective_from (YYYY-MM-DD) and
    window_days (0 to 14, default 3: how many days apart the two legs may
    post) narrow or widen it."""

    def op(session: Session) -> dict[str, Any]:
        from datetime import date as date_type

        from purser import ledger as ledger_service
        from purser import rules as rules_service
        from purser.core import rules as core_rules

        since = date_type.fromisoformat(effective_from) if effective_from else None
        exact = Decimal(amount_exact) if amount_exact else None
        if pair_accounts is not None:
            if len(pair_accounts) != 2:
                return {"error": "pair_accounts needs exactly two account references"}
            if any((match_field, match_mode, match_text, account, direction)):
                return {
                    "error": "a transfer_link rule's pair is its scope: only amount_exact, "
                    "effective_from and window_days narrow or widen it"
                }
            try:
                return rules_service.preview_links(
                    session,
                    pair_account_a=ledger_service.resolve_account(session, pair_accounts[0]).id,
                    pair_account_b=ledger_service.resolve_account(session, pair_accounts[1]).id,
                    amount_exact=exact,
                    effective_from=since,
                    window_days=window_days,
                    limit=_SAMPLE_CAP,
                )
            except rules_service.RuleError as exc:
                return {"error": str(exc)}
        if window_days is not None:
            return {"error": "window_days applies only to transfer_link rules: pass pair_accounts"}

        account_ids = None
        if account:
            account_ids = frozenset({str(ledger_service.resolve_account(session, account).id)})
        predicate = core_rules.Predicate(
            account_ids=account_ids,
            match_field=core_rules.MatchField(match_field) if match_field else None,
            match_mode=core_rules.MatchMode(match_mode) if match_mode else None,
            match_text=match_text,
            amount_exact=exact,
            direction=core_rules.Direction(direction) if direction else None,
            effective_from=since,
        )
        return rules_service.preview(session, predicate=predicate, limit=_SAMPLE_CAP)

    return await _run(op)


@audited_tool(kind="write", entity="rule", scope="write", emits=Emits(payee=True, rule_text=True))
async def propose_rule(
    type: str,
    category: str | None = None,
    payee_name: str | None = None,
    pair_accounts: list[str] | None = None,
    window_days: int | None = None,
    match_field: str | None = None,
    match_mode: str | None = None,
    match_text: str | None = None,
    account: str | None = None,
    amount_exact: str | None = None,
    direction: str | None = None,
    effective_from: str | None = None,
    note: str | None = None,
    priority: int = 100,
    activate: bool = False,
) -> dict[str, Any]:
    """Create a deterministic rule. By default it lands as a DRAFT for the
    user to review and never executes until activated. Pass activate=true
    when the user asked for this rule; leave it false when the rule is your
    own idea (a pattern you noticed), so the user decides. A transfer_link
    rule is always a draft: the user activates it after confirming how many
    historical pairs it would link.
    type is categorize | rename | transfer_link. For categorize, `category`
    may be a category path OR one of the user's own account paths, which makes
    the rule declare a transfer. A transfer_link names pair_accounts (two
    account references) and may set window_days, 0 to 14 (default 3): how
    many days apart the pair's two legs may post and still link. Keep it as
    narrow as the bank's actual delay — a wider window reaches more history.
    Preview first."""

    def op(session: Session) -> dict[str, Any]:
        from datetime import date as date_type

        from purser import ledger as ledger_service
        from purser import rules as rules_service
        from purser.api.rules import _Accounts, _render_action, _render_predicate, window_of

        token = _token_row(session, _principal())
        fields: dict[str, Any] = {"type": type, "priority": priority}
        if category:
            fields["category_id"] = ledger_service.resolve_account(session, category.strip("[]")).id
        if payee_name:
            fields["payee_name"] = payee_name
        if pair_accounts:
            if len(pair_accounts) != 2:
                return {"error": "pair_accounts needs exactly two account references"}
            fields["pair_account_a"] = ledger_service.resolve_account(session, pair_accounts[0]).id
            fields["pair_account_b"] = ledger_service.resolve_account(session, pair_accounts[1]).id
        if account:
            fields["account_ids"] = [ledger_service.resolve_account(session, account).id]
        for key, value in (
            ("match_field", match_field),
            ("match_mode", match_mode),
            ("match_text", match_text),
            ("direction", direction),
            ("note", note),
        ):
            if value:
                fields[key] = value
        if amount_exact:
            fields["amount_exact"] = Decimal(amount_exact)
        if effective_from:
            fields["effective_from"] = date_type.fromisoformat(effective_from)
        if window_days is not None:
            fields["window_days"] = window_days

        try:
            rule = rules_service.create(session, fields=fields, token=token, activate=activate)
        except rules_service.RuleError as exc:
            return {"error": str(exc)}
        accounts = _Accounts(session)
        window = window_of(rule)
        return {
            "id": str(rule.id),
            "state": rule.state,
            "when": _render_predicate(rule, accounts),
            "then": _render_action(rule, accounts),
            **({"window_days": window} if window is not None else {}),
            "note": (
                "active — it runs on new and uncategorized rows"
                if rule.state == rules_service.STATE_ACTIVE
                else "draft — the user must activate it before it runs"
            ),
        }

    return await _run(op)
