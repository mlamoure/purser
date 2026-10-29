"""Deterministic rules API (issue #174, DESIGN decision 46).

Every write is ``write`` scope (#436). A rule is created as a DRAFT unless
the caller asks for ``activate`` — a draft never executes, because
``rules.load_active`` is the only loader and filters ``state='active'``. An
agent acting on its own initiative leaves it a draft for the user to decide.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import rules as rules_service
from purser.admin.auth import ReadToken, WriteToken
from purser.core import matching as core_matching
from purser.core import rules as core_rules
from purser.db import get_session
from purser.models import Rule

DbSession = Annotated[Session, Depends(get_session)]

# Strict: a window is a whole number of days, so 5.5, "5" and true are all
# refused here (422) rather than coerced (decision 66).
WindowDays = Annotated[int, Field(strict=True, ge=0, le=core_matching.TRANSFER_WINDOW_MAX_DAYS)]
_WINDOW_DAYS_DOC = (
    "transfer_link rules only: the most days apart the declared pair's two legs may post "
    f"and still link, 0 to {core_matching.TRANSFER_WINDOW_MAX_DAYS}. Omit for the standard "
    f"{core_matching.TRANSFER_WINDOW_DAYS}-day window; 0 means same-day legs only. Widening "
    "it puts more history in scope at activation, which the activation confirm counts."
)


class _Accounts:
    """Path lookup plus the real-vs-virtual split, fetched once per response.

    The split matters for rendering: a `categorize` rule may legitimately name
    a REAL account, and that has to render bracketed so the UI reads like a
    manual transfer categorization rather than an ordinary category.
    """

    def __init__(self, session: Session) -> None:
        from purser.models import Account

        rows = session.execute(select(Account.id, Account.path, Account.is_virtual)).all()
        self._paths: dict[uuid.UUID, str] = {row[0]: str(row[1]) for row in rows}
        self.real = frozenset(row[0] for row in rows if not row[2])
        self._session = session
        self._payees: dict[uuid.UUID, str] = {}

    def get(self, account_id: uuid.UUID | None, default: str = "?") -> str:
        if account_id is None:
            return default
        return self._paths.get(account_id, str(account_id))

    def payee(self, payee_id: uuid.UUID) -> str:
        """A payee scope's name as it reads TODAY — the surviving payee of its
        alias group, since that group is what the scope covers (decision 67).
        Looked up lazily: only payee-scoped transfer_link rules have one."""
        if payee_id not in self._payees:
            from purser.models import Payee

            resolved = rules_service.resolve_payee_id(self._session, payee_id)
            row = self._session.get(Payee, resolved) if resolved is not None else None
            self._payees[payee_id] = row.name if row is not None else str(payee_id)
        return self._payees[payee_id]


router = APIRouter(prefix="/rules", tags=["rules"])


def _handle(exc: rules_service.RuleError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


class RulePredicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ids: list[uuid.UUID] | None = None
    match_field: Literal["payee", "description", "payee_or_description"] | None = None
    match_mode: Literal["equals", "prefix", "contains"] | None = None
    match_text: str | None = None
    amount_exact: Decimal | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    direction: Literal["debit", "credit"] | None = None
    effective_from: date | None = None
    window_days: WindowDays | None = Field(default=None, description=_WINDOW_DAYS_DOC)


class RuleCreate(RulePredicate):
    type: Literal["categorize", "rename", "transfer_link"]
    priority: int = 100
    category_id: uuid.UUID | None = None
    payee_name: str | None = None
    pair_account_a: uuid.UUID | None = None
    pair_account_b: uuid.UUID | None = None
    note: str | None = None
    # Draft by default; a write principal carrying out the user's request may
    # create the rule already active (#436).
    activate: bool = False


class RuleUpdate(RulePredicate):
    priority: int | None = None
    category_id: uuid.UUID | None = None
    payee_name: str | None = None
    pair_account_a: uuid.UUID | None = None
    pair_account_b: uuid.UUID | None = None
    note: str | None = None


class RuleOut(BaseModel):
    id: uuid.UUID
    type: str
    state: str
    priority: int
    predicate: str
    action: str
    applied_count: int
    note: str | None
    window_days: int | None = Field(
        description=(
            "transfer_link rules: the date window the declared pair links within "
            f"({core_matching.TRANSFER_WINDOW_DAYS}, the standard window, when the rule "
            "sets none). Null for other types."
        )
    )
    payee_scope: str | None = Field(
        default=None,
        description=(
            "transfer_link rules accepted from a transfer-link suggestion: the payee one of "
            "the pair's legs must carry — that payee or any alias of it. Null = the whole pair."
        ),
    )

    @classmethod
    def from_model(cls, rule: Rule, paths: _Accounts) -> "RuleOut":
        return cls(
            id=rule.id,
            type=rule.type,
            state=rule.state,
            priority=rule.priority,
            predicate=_render_predicate(rule, paths),
            action=_render_action(rule, paths),
            applied_count=rule.applied_count,
            note=rule.note,
            window_days=window_of(rule),
            payee_scope=payee_scope_of(rule, paths),
        )


def window_of(rule: Rule) -> int | None:
    """A transfer_link rule's effective window; None for every other type."""
    if rule.type != rules_service.TYPE_TRANSFER_LINK:
        return None
    return rules_service.effective_window_days(rule.window_days)


def payee_scope_of(rule: Rule, paths: _Accounts) -> str | None:
    """The payee a transfer_link rule is scoped to (decision 67); None when it
    declares the whole pair, and for every other type."""
    if rule.type != rules_service.TYPE_TRANSFER_LINK or rule.payee_scope_id is None:
        return None
    return paths.payee(rule.payee_scope_id)


def _render_predicate(rule: Rule, paths: _Accounts) -> str:
    """One line a human can check against an audit row.

    This is the readability argument for a structured predicate made concrete:
    `payee contains "amazon"` tells a user what the rule does; a regex does not.
    """
    parts: list[str] = []
    if rule.account_ids:
        parts.append("account in " + ", ".join(paths.get(a) for a in rule.account_ids))
    if rule.match_field and rule.match_mode and rule.match_text:
        parts.append(f'{rule.match_field} {rule.match_mode} "{rule.match_text}"')
    if rule.amount_exact is not None:
        parts.append(f"amount = {rule.amount_exact}")
    if rule.amount_min is not None:
        parts.append(f"amount >= {rule.amount_min}")
    if rule.amount_max is not None:
        parts.append(f"amount <= {rule.amount_max}")
    if rule.direction:
        parts.append(rule.direction)
    if rule.effective_from:
        parts.append(f"on or after {rule.effective_from.isoformat()}")
    scope = payee_scope_of(rule, paths)
    if scope is not None:
        parts.append(f'a leg\'s payee is "{scope}" or an alias')
    window = window_of(rule)
    if window is not None:
        # A declared pair always has a window, so it always says which one.
        parts.append(f"legs <= {window} days apart")
    return " AND ".join(parts) if parts else "every row"


def _render_action(rule: Rule, paths: _Accounts) -> str:
    if rule.type == rules_service.TYPE_CATEGORIZE and rule.category_id:
        path = paths.get(rule.category_id)
        # A REAL-account target renders in brackets so the UI reads exactly
        # like a manual transfer categorization (decision 46, clarification 2).
        if rule.category_id in paths.real:
            return f"-> [{path}]"
        return f"-> {path}"
    if rule.type == rules_service.TYPE_RENAME:
        return f'-> payee "{rule.payee_name}"'
    a = paths.get(rule.pair_account_a)
    b = paths.get(rule.pair_account_b)
    return f"link {a} <-> {b}"


@router.get("")
def list_rules(
    session: DbSession,
    _: ReadToken,
    state: str | None = None,
    type: str | None = None,
) -> list[RuleOut]:
    """Rules in EVALUATION order, so resolution is visible before it fires."""
    stmt = select(Rule).order_by(Rule.priority, Rule.created_at, Rule.id)
    if state:
        stmt = stmt.where(Rule.state == state)
    if type:
        stmt = stmt.where(Rule.type == type)
    rows = list(session.execute(stmt).scalars())
    paths = _Accounts(session)
    return [RuleOut.from_model(r, paths) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleCreate, token: WriteToken, session: DbSession) -> RuleOut:

    fields = body.model_dump(exclude_none=True, exclude={"activate"})
    activate = body.activate
    try:
        rule = rules_service.create(session, fields=fields, token=token, activate=activate)
    except rules_service.RuleError as exc:
        raise _handle(exc) from exc
    session.commit()
    return RuleOut.from_model(rule, _Accounts(session))


@router.patch("/{rule_id}")
def update_rule(
    rule_id: uuid.UUID, body: RuleUpdate, token: WriteToken, session: DbSession
) -> RuleOut:
    try:
        rule = rules_service.update(
            session, rule_id=rule_id, changes=body.model_dump(exclude_unset=True), token=token
        )
    except rules_service.RuleError as exc:
        raise _handle(exc) from exc
    session.commit()
    return RuleOut.from_model(rule, _Accounts(session))


class StateChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["draft", "active", "disabled", "archived"]
    # Activating a transfer_link rule is drift-blind over all history, so the
    # projected link count must be confirmed explicitly (decision 46, Q2).
    confirm_links: int | None = None


@router.post("/{rule_id}/state")
def set_rule_state(
    rule_id: uuid.UUID, body: StateChange, token: WriteToken, session: DbSession
) -> RuleOut:
    try:
        rule = rules_service.set_state(
            session,
            rule_id=rule_id,
            state=body.state,
            token=token,
            confirmed_links=body.confirm_links,
        )
    except rules_service.RuleError as exc:
        raise _handle(exc) from exc
    session.commit()
    return RuleOut.from_model(rule, _Accounts(session))


@router.delete("/{rule_id}")
def delete_rule(rule_id: uuid.UUID, token: WriteToken, session: DbSession) -> RuleOut:
    """DELETE maps to ARCHIVED, never a hard delete — audit rows reference the
    id and must stay resolvable."""
    try:
        rule = rules_service.set_state(
            session, rule_id=rule_id, state=rules_service.STATE_ARCHIVED, token=token
        )
    except rules_service.RuleError as exc:
        raise _handle(exc) from exc
    session.commit()
    return RuleOut.from_model(rule, _Accounts(session))


class RulePreview(RulePredicate):
    # Both set: preview a transfer_link rule for this pair instead of a
    # predicate — the projected link count the activation confirm asks for.
    pair_account_a: uuid.UUID | None = None
    pair_account_b: uuid.UUID | None = None


@router.post("/preview")
def preview_rule(body: RulePreview, _: ReadToken, session: DbSession) -> dict[str, Any]:
    """Dry-run a rule over history before creating it.

    With a predicate: how many live rows it matches, plus a small sample.
    With `pair_account_a` and `pair_account_b`: a transfer_link rule for that
    pair (`amount_exact`, `effective_from` and `window_days` narrow or widen
    it) — `links` is exactly the count activating such a rule would ask you
    to confirm, with a sample of the pairs it would link."""
    pair = (body.pair_account_a, body.pair_account_b)
    if any(account is not None for account in pair):
        if body.pair_account_a is None or body.pair_account_b is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="a transfer_link preview needs both pair_account_a and pair_account_b",
            )
        text_scope = (
            body.account_ids,
            body.match_field,
            body.match_mode,
            body.match_text,
            body.amount_min,
            body.amount_max,
            body.direction,
        )
        if any(field is not None for field in text_scope):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "a transfer_link rule's pair is its scope: only amount_exact, "
                    "effective_from and window_days narrow or widen it"
                ),
            )
        try:
            return rules_service.preview_links(
                session,
                pair_account_a=body.pair_account_a,
                pair_account_b=body.pair_account_b,
                amount_exact=body.amount_exact,
                effective_from=body.effective_from,
                window_days=body.window_days,
            )
        except rules_service.RuleError as exc:
            raise _handle(exc) from exc
    if body.window_days is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="window_days applies only to transfer_link rules: pass the account pair",
        )
    predicate = core_rules.Predicate(
        account_ids=frozenset(str(a) for a in body.account_ids) if body.account_ids else None,
        match_field=core_rules.MatchField(body.match_field) if body.match_field else None,
        match_mode=core_rules.MatchMode(body.match_mode) if body.match_mode else None,
        match_text=body.match_text,
        amount_exact=body.amount_exact,
        amount_min=body.amount_min,
        amount_max=body.amount_max,
        direction=core_rules.Direction(body.direction) if body.direction else None,
        effective_from=body.effective_from,
    )
    return rules_service.preview(session, predicate=predicate)


class ApplyHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    posted: Literal["skip", "correct"] = "skip"
    include_user_categorized: bool = Field(
        default=False,
        description=("Automation correcting automation is fine; overriding a human is opt-in."),
    )


@router.post("/{rule_id}/apply-history")
def apply_history(
    rule_id: uuid.UUID, body: ApplyHistory, token: WriteToken, session: DbSession
) -> dict[str, Any]:
    try:
        result = rules_service.apply_history(
            session,
            rule_id=rule_id,
            token=token,
            dry_run=body.dry_run,
            posted=body.posted,
            include_user_categorized=body.include_user_categorized,
        )
    except rules_service.RuleError as exc:
        raise _handle(exc) from exc
    session.commit()
    return result
