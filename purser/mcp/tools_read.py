"""Compact read tools: orientation, accounts, transactions, lookups.

Registration happens on import (the ``@audited_tool`` decorator calls
``mcp.tool``), so this module is imported for its side effects by
``purser.mcp.server``. Adding a tool here needs no wiring beyond that.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from purser import clock, ledger
from purser.mcp._registry import _amount, _principal, _run, audited_tool
from purser.privacy import NOTHING, Emits


def _balance_of(row: Any) -> Decimal:
    """A ``networth.ReportedBalance``'s balance, 0 for an account with no row."""
    return Decimal(0) if row is None else row.balance


def _method_field(row: Any, *, with_as_of: bool = True) -> dict[str, Any]:
    """`balance_method` (+ `balance_as_of`) keys for a
    ``networth.ReportedBalance``, present ONLY on overlay-valued accounts —
    plain postings accounts stay lighter (token budget). ``describe`` passes
    ``with_as_of=False``: it is the every-session orientation call, so it
    carries the method and leaves the date to
    ``list_accounts``/``account_balance``."""
    if row is None or row.as_of is None:
        return {}
    return {
        "balance_method": row.method,
        **({"balance_as_of": row.as_of.isoformat()} if with_as_of else {}),
    }


def _memories_summary(session: Session) -> dict[str, Any]:
    from purser import memories as memories_service

    return {"memories": memories_service.serving_block(session)}


def _review_summary(session: Session) -> dict[str, Any]:
    """describe() carries COUNTS and a tool pointer, never rows (decision
    141): how many transactions need review, by reason."""
    from purser import review as review_service

    counts = review_service.counts(session)
    return {
        "needs_review": counts["total"],
        "uncategorized": counts[review_service.REASON_UNCATEGORIZED],
        "reconciliation_adjustments": counts[review_service.REASON_ADJUSTMENT],
        "tool": "review_batch",
    }


@audited_tool(
    kind="read",
    entity="system",
    scope="read",
    emits=Emits(payee=True, skip=("conventions", "aggregate_tools", "reach")),
)
async def describe() -> dict[str, Any]:
    """Orient in one round-trip: data model, conventions, scopes, how Purser
    is reached (this_machine, home_network, reverse_proxy or unknown), category
    tree, and account list. Call this first in a fresh session."""

    def op(session: Session) -> dict[str, Any]:
        from purser import network
        from purser import networth as networth_service
        from purser.config import get_settings
        from purser.models import Account

        principal = _principal()
        settings = get_settings()
        accounts = list(
            session.execute(
                select(Account).where(Account.is_virtual.is_(False)).order_by(Account.path)
            ).scalars()
        )
        reported = networth_service.reported_balances(session)
        categories = [
            a.path
            for a in session.execute(
                select(Account)
                .where(Account.is_virtual.is_(True), Account.type.in_(ledger.CATEGORY_TYPES))
                .order_by(Account.path)
            ).scalars()
        ]
        return {
            "conventions": {
                "amounts": "decimal strings; negative = money out of the account",
                "balances": (
                    "loan balances are the latest valuation "
                    "(marked balance_method='valuation'); a hand-valued asset "
                    "(type property) is a ledger changed by record_value_change "
                    "and set on a date by reconcile_account; brokerage adds its "
                    "latest holdings market value to the postings sum "
                    "(balance_method='postings+holdings'; holdings come from the "
                    "feed or, for holdings_source=manual, from record_position); "
                    "everything else is the postings sum"
                ),
                "spending": (
                    "spending_summary totals are positive-for-spend (category-leg "
                    "convention); search_transactions amounts are negative-for-spend."
                ),
                "history": (
                    "balance over time: ONE account = balance_history(account, "
                    "period); whole portfolio = net_worth(period); forward-looking "
                    "= forecast_balance(account, days). Never reconstruct a "
                    "balance series by walking transactions."
                ),
                "lifecycle": "expected -> pending -> posted -> void; posted is immutable",
                "writes": (
                    "write scope: you act with the user's authority, so write "
                    "directly for the user's request; on your own initiative "
                    "propose instead (activate=false on propose_rule/"
                    "propose_memory, propose=true on create_schedule). Every "
                    "write is audited and reversible"
                ),
                "references": "accounts/categories accept UUID or ltree path",
                "disclosure": (
                    "your choice per call: when the question or action does not "
                    "need them, pass withhold=[payee, description, amount] (any "
                    "subset); payees then arrive as py_ references and `withheld` "
                    "names what was left out. Nothing is withheld unless you ask. "
                    "Filter and act with payee references; present(transaction_ids) "
                    "shows the user details without you repeating them. Aggregates "
                    "are always exact"
                ),
                "transfers": (
                    "one transaction with two real-account postings; "
                    "update_transaction category='[account path]' converts a row "
                    "into a transfer to that account; a split loan payment "
                    "(confirm_transfer remainder_category) adds one interest/fee posting"
                ),
            },
            "your_scopes": list(principal.scopes),
            # One word (#632): whether other machines can reach this Purser.
            "reach": network.reach_mode(settings.public_url, settings.bind_address),
            "accounts": [
                {
                    "path": a.path,
                    "type": a.type,
                    "currency": a.currency.strip(),
                    "balance": _amount(_balance_of(reported.get(a.id))),
                    "hidden": a.hidden_at is not None,
                    "closed": a.closed_at is not None,
                    # key present only on overlay-valued accounts (token budget)
                    **_method_field(reported.get(a.id), with_as_of=False),
                }
                for a in accounts
            ],
            "categories": categories,
            # Pointers, not payloads: a count plus the tool that serves it.
            "review": _review_summary(session),
            **_memories_summary(session),
            "aggregate_tools": [
                "spending_summary",
                "net_worth",
                "balance_history",
                "forecast_balance",
                "list_recurring",
                "whats_new",
                "list_unmatched",
            ],
        }

    return await _run(op)


def _reconciliation_field(check: Any) -> dict[str, Any]:
    """`reconciliation` key for a reconciled account (absent otherwise — token
    budget): its latest reconciliation, re-checked against the ledger now."""
    if check is None:
        return {}
    row = check.reconciliation
    return {
        "reconciliation": {
            "as_of": row.as_of.isoformat(),
            "basis": row.basis,
            "component": check.component,
            "in_balance": check.in_balance,
            "difference": _amount(check.difference),
        }
    }


def _iso(day: Any) -> str | None:
    return day.isoformat() if day is not None else None


@audited_tool(
    kind="read", entity="account", scope="read", emits=Emits(external_ids=("[].external_id",))
)
async def list_accounts(include_balances: bool = True) -> list[dict[str, Any]]:
    """Real accounts with current balances (pending+posted). Loan accounts
    report their latest valuation instead of the postings sum
    (marked balance_method='valuation'); brokerage accounts add their latest
    holdings market value on top of it (balance_method='postings+holdings').
    Both carry the overlay's balance_as_of date. A reconciled account carries
    `reconciliation`: its latest reconciled date, basis and component (cash
    only for brokerage, balance for other accounts), and whether the
    ledger still matches the statement on that date (difference = ledger
    minus statement target, ledger sign; nonzero means a later write moved
    it). A hand-valued asset (type property) carries asset_kind (house, car,
    ...) and, when it secures loans, equity (its value plus those loans,
    ledger sign) with secured_loans (their paths); a secured loan carries
    secured_by (the asset's path). An asset whose value is older than its
    revalue_after_days carries value_stale=true and last_valued. Every asset
    carries gain (the sum of its changes in value) and money_put_in (its
    purchase and transfers); together they are its value."""

    def op(session: Session) -> list[dict[str, Any]]:
        from purser import assets as assets_service
        from purser import networth as networth_service
        from purser import reconciliation as reconciliation_service
        from purser.models import Account

        accounts = list(
            session.execute(
                select(Account).where(Account.is_virtual.is_(False)).order_by(Account.path)
            ).scalars()
        )
        reported = networth_service.reported_balances(session) if include_balances else {}
        checks = reconciliation_service.latest_checks(session, accounts)
        paths = {a.id: a.path for a in accounts}
        asset_ids = [a.id for a in accounts if a.type in assets_service.ASSET_TYPES]
        asset_rows = assets_service.details_for(session, asset_ids)
        kinds = {account_id: row.kind for account_id, row in asset_rows.items()}
        sums = assets_service.gains(session, asset_ids)
        last = assets_service.last_valued(session, asset_ids)
        today = clock.ledger_today()
        stale = {
            account_id
            for account_id, row in asset_rows.items()
            if assets_service.is_stale(last.get(account_id), row.revalue_after_days, today)
        }
        equity = (
            assets_service.equities(session, accounts, {k: v.balance for k, v in reported.items()})
            if include_balances
            else {}
        )
        return [
            {
                "id": str(a.id),
                "path": a.path,
                "name": a.name,
                "type": a.type,
                "balance": (_amount(_balance_of(reported.get(a.id))) if include_balances else None),
                # Omitted for agents unless included (#437; scrubbed centrally).
                "external_id": a.external_id,
                "hidden": a.hidden_at is not None,
                "closed": a.closed_at is not None,
                # keys present only on overlay-valued accounts (token budget)
                **(_method_field(reported.get(a.id)) if include_balances else {}),
                # present only on reconciled accounts (token budget)
                **_reconciliation_field(checks.get(a.id)),
                # hand-valued assets and secured loans only (#567)
                **(
                    {
                        "asset_kind": kinds.get(a.id, "other"),
                        "gain": _amount(sums[a.id][1]),
                        "money_put_in": _amount(sums[a.id][0]),
                    }
                    if a.id in kinds
                    else {}
                ),
                **(
                    {
                        "equity": _amount(equity[a.id][0]),
                        "secured_loans": [paths.get(i, str(i)) for i in equity[a.id][1]],
                    }
                    if a.id in equity and equity[a.id][1]
                    else {}
                ),
                **(
                    {"secured_by": paths.get(a.secured_by_id, str(a.secured_by_id))}
                    if a.secured_by_id
                    else {}
                ),
                **(
                    {
                        "value_stale": True,
                        "last_valued": _iso(last.get(a.id)),
                    }
                    if a.id in stale and a.closed_at is None
                    else {}
                ),
            }
            for a in accounts
        ]

    return await _run(op)


@audited_tool(kind="read", entity="connection", scope="read", emits=NOTHING)
async def feed_status() -> dict[str, Any]:
    """Bank feed health in one look: one line per bank login (status ok,
    needs_login — log in again at the bank through SimpleFIN Bridge — or
    error, with how many accounts it delivers and how many feed a ledger
    account), then only the accounts that need attention: not_connected (a
    feed account no ledger account takes, e.g. a newly opened one; one the
    user chose to ignore is not listed), stale Nd
    (the bank balance has not updated for N days), an act.* code (the bank
    failed to deliver it), ambiguous_id. Account numbers are never shown.
    Use it for "did a new account show up?" or "which bank needs me?". A
    not_connected account carries a ref: map_feed_accounts maps it."""

    def op(session: Session) -> dict[str, Any]:
        from purser.feed import inventory

        return inventory.feed_status(session)

    return await _run(op)


@audited_tool(kind="read", entity="account", scope="read", emits=NOTHING)
async def account_balance(account: str, as_of: str | None = None) -> dict[str, Any]:
    """Point-in-time balance of one real account (UUID or path). Open
    loan accounts answer with their latest valuation at or before
    as_of (method 'valuation'); open brokerage accounts add their latest
    holdings market value on top of the postings basis (method
    'postings+holdings'); everything else answers from the STORED running
    balances alone (method 'postings'; register semantics: chain-head
    pending+posted rows). as_of is an ISO date, default today; 0 before the
    account's first activity; closed accounts answer their whole history.
    bank_snapshot carries the latest bank-reported and available balances,
    their currency, source timestamp/precision, ingestion time and freshness,
    under the same read scope. Available balance is separate (e.g. remaining
    credit on a card). Its comparison reports live posted and pending-inclusive
    totals over ALL recorded dates, the effective/learned/pinned basis and exact
    bank-minus-ledger difference; matches_either_basis is the separate alert
    policy. Null means unknown/unavailable, never zero. Even with as_of, this
    snapshot is LATEST, not historical bank truth. For a balance over TIME,
    call balance_history instead of looping this."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser import networth as networth_service
        from purser.feed.snapshot import account_snapshot

        target = ledger_service.resolve_account(session, account)
        if target.is_virtual:
            raise ledger_service.LedgerError(
                f"{target.path} is a category; balances apply to real accounts"
            )
        effective = date.fromisoformat(as_of) if as_of else clock.ledger_today()
        balance, method, overlay_as_of = networth_service.account_balance_at(
            session, target, effective
        )
        return {
            "account": target.path,
            "as_of": effective.isoformat(),
            "balance": _amount(balance),
            "method": method,
            "currency": target.currency.strip(),
            "bank_snapshot": account_snapshot(session, target).model_dump(mode="json"),
            # present only when an overlay produced the answer (token budget)
            **({"balance_as_of": overlay_as_of.isoformat()} if overlay_as_of else {}),
        }

    return await _run(op)


@audited_tool(
    kind="read",
    entity="transaction",
    scope="read",
    emits=Emits(
        payee=True,
        description=("did_you_mean",),
        amount=("items[].amount", "items[].remainder"),
        balance=("items[].balance",),
    ),
)
async def search_transactions(
    account: str | None = None,
    category: str | None = None,
    payee: str | None = None,
    q: str | None = None,
    start: str | None = None,
    end: str | None = None,
    status: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered, paginated flat transactions (compact). When `account` names a
    real account each row also carries `balance` — the stored running balance
    after that transaction from that account's perspective, the same number
    the register's Balance column shows. It is RECORDED, not computed: never
    derive a balance by summing rows. For totals use spending_summary — do
    not sum these rows yourself.

    Transfers carry `counterparty` (the other account's path). Under a
    real-account perspective their `amount` is that account's leg (negative =
    money left it); with no perspective it is the outgoing leg's amount and
    `counterparty` is the receiving account. A plain transfer has no
    category; a split loan payment (confirm_transfer with remainder_category)
    carries its interest/fee category as `category` and that posting's amount
    as `remainder`. Other multi-posting rows are not transfers. A split row
    carries `split` — its number of lines (categories and [account] lines)
    — and no `category`, and is never a transfer; get_transaction lists its
    lines, split_transaction edits them.

    q: words all found in payee, description or memo ("phrase"; -word
    excludes; word~ allows one typo; payee: desc: memo: scope one;
    payee:py_... is that payee), plus account:, category:, amount:>100 or
    10..20 (magnitude; is:debit|credit), date:2026-10 or 2026-01..2026-03,
    status:pending,posted, is:needs-review. One word with no hits may add
    did_you_mean."""
    from purser import privacy

    withheld = privacy.current_withheld.get() or set()

    def op(session: Session) -> dict[str, Any]:
        from purser import balances as balance_service
        from purser import ledger as ledger_service
        from purser import transfers as transfer_service
        from purser.core import ledger as core

        rows, total = ledger_service.search_transactions(
            session,
            account_ref=account,
            category_subtree=category,
            payee=payee,
            q=q,
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            statuses=status,
            limit=min(limit, 50),
            offset=offset,
        )
        # A perspective account is what makes a running balance meaningful:
        # the same transaction has a different balance in each account it
        # touches. Categories are virtual and have no running-balance rows
        # (DESIGN §3.5), so they get no column at all rather than a column of
        # nulls. resolve_account is safe to call here — search_transactions
        # already resolved the same ref, so an unknown one raised above.
        perspective_id: uuid.UUID | None = None
        balances_map: dict[uuid.UUID, Decimal] = {}
        if account is not None:
            target = ledger_service.resolve_account(session, account)
            if not target.is_virtual:
                perspective_id = target.id
                balances_map = balance_service.balances_for_transactions(
                    session, target.id, [t.id for t in rows]
                )
        items = []
        for t in rows:
            real = [p for p in t.postings if not p.account.is_virtual]
            virtual = [p for p in t.postings if p.account.is_virtual]
            # Shape, not leg count (decision 62): the same classifier as the
            # REST flat view, so a multi-posting row is never a transfer here.
            shape = transfer_service.transfer_shape(t)
            splits = transfer_service.split_lines(t)
            # The leg this row is seen through (#232): the perspective
            # account's for a register view, else the only real leg, else —
            # for a transfer with no perspective — the outgoing (payer) leg.
            if perspective_id is not None:
                leg = next((p for p in real if p.account_id == perspective_id), None)
            elif t.split_account_id is not None:
                # A split is seen through its own leg (decision 118).
                leg = next((p for p in real if p.account_id == t.split_account_id), None)
            elif len(real) == 1:
                leg = real[0]
            elif shape is not None:
                leg = min(real, key=lambda p: Decimal(p.amount))
            else:
                leg = None
            counterparty = (
                next((p.account.path for p in real if leg is not None and p.id != leg.id), None)
                if shape is not None
                else None
            )
            items.append(
                {
                    "id": str(t.id),
                    "date": t.date.isoformat(),
                    "amount": _amount(leg.amount) if leg is not None else None,
                    "payee": t.payee.name if t.payee else None,
                    "category": virtual[0].account.path
                    if len(virtual) == 1 and not splits
                    else None,
                    "status": t.status,
                    # Keys present only on transfers / split loan payments,
                    # so flat rows keep the pinned compact shape.
                    **({"counterparty": counterparty} if counterparty is not None else {}),
                    # Present only on split rows (decision 87).
                    **({"split": splits} if splits else {}),
                    **(
                        {"remainder": _amount(virtual[0].amount)}
                        if shape is core.TransferShape.SPLIT
                        else {}
                    ),
                    # Key present only under a real-account perspective; null
                    # within it for rows the store does not cover (void,
                    # expected). Matches TransactionOut.balance byte for byte.
                    **(
                        {"balance": _amount(balances_map.get(t.id))}
                        if perspective_id is not None
                        else {}
                    ),
                }
            )
        out: dict[str, Any] = {"total": total, "returned": len(items), "items": items}
        if q and total == 0 and not withheld & {"payee", "description"}:
            suggestion = ledger_service.did_you_mean(session, q)
            if suggestion is not None:
                out["did_you_mean"] = suggestion
        return out

    return await _run(op)


@audited_tool(
    kind="read",
    entity="transaction",
    scope="read",
    emits=Emits(
        payee=True,
        description=("description", "postings[].memo", "split.lines[].memo"),
        amount=("postings[].amount", "split.lines[].amount"),
    ),
)
async def get_transaction(transaction_id: str) -> dict[str, Any]:
    """Full detail for one transaction: postings and provenance links. A
    split also carries `split`: its account and its lines ({category,
    amount, memo?, backing_ids?} — the shape split_transaction takes, so an
    edit can start from it; backing_ids are the bank rows merged into an
    account line)."""

    def op(session: Session) -> dict[str, Any]:
        from purser import ledger as ledger_service
        from purser import splits

        t = ledger_service.get_transaction(session, uuid.UUID(transaction_id))
        split: dict[str, Any] = {}
        if t.split_account_id is not None:
            own = splits.own_leg(t)
            split = {
                "split": {
                    "account": own.account.path if own is not None else None,
                    "lines": [
                        {
                            "category": line["target"],
                            "amount": _amount(line["amount"]),
                            **({"memo": line["memo"]} if line["memo"] else {}),
                            **(
                                {"backing_ids": [str(i) for i in line["backing_ids"]]}
                                if line["kind"] == "account"
                                else {}
                            ),
                        }
                        for line in splits.describe_lines(session, t)
                    ],
                }
            }
        return {
            "id": str(t.id),
            "date": t.date.isoformat(),
            "status": t.status,
            "origin": t.origin,
            "payee": t.payee.name if t.payee else None,
            "description": t.description,
            "postings": [
                {
                    "account": p.account.path,
                    "amount": _amount(p.amount),
                    "currency": p.currency.strip(),
                }
                for p in t.postings
            ],
            "provenance": {
                "content_hash": t.content_hash,
                "schedule_id": str(t.schedule_id) if t.schedule_id else None,
                "source_fetch_id": str(t.source_fetch_id) if t.source_fetch_id else None,
                "superseded_by": str(t.superseded_by) if t.superseded_by else None,
                "reverses_id": str(t.reverses_id) if t.reverses_id else None,
            },
            **split,
        }

    return await _run(op)


@audited_tool(kind="read", entity="category", scope="read", emits=NOTHING)
async def list_categories(include_descriptions: bool = False) -> list[str] | list[dict[str, Any]]:
    """The category tree as ltree paths (categories are virtual accounts).

    include_descriptions=true returns {path, description} objects instead —
    heavier on tokens; the bare path list is usually enough."""

    def op(session: Session) -> list[str] | list[dict[str, Any]]:
        from purser.models import Account

        accounts = session.execute(
            select(Account)
            .where(Account.is_virtual.is_(True), Account.type.in_(ledger.CATEGORY_TYPES))
            .order_by(Account.path)
        ).scalars()
        if include_descriptions:
            return [{"path": a.path, "description": a.description} for a in accounts]
        return [a.path for a in accounts]

    return await _run(op)


@audited_tool(
    kind="read",
    entity="schedule",
    scope="read",
    emits=Emits(
        payee=True, narrative=True, description=("[].name", "[].memo"), amount=("[].amount",)
    ),
)
async def list_schedules(status: str | None = None) -> list[dict[str, Any]]:
    """Bill/income schedules with next due dates. name is the label (memo,
    else payee, else the transfer counterparty); payee and memo are the
    fields create_schedule and update_schedule take."""

    def op(session: Session) -> list[dict[str, Any]]:
        from purser.models import Account, Schedule

        stmt = select(Schedule).options(selectinload(Schedule.payee)).order_by(Schedule.next_due)
        if status is not None:
            stmt = stmt.where(Schedule.status == status)
        schedules = list(session.execute(stmt).scalars())
        account_ids = {s.account_id for s in schedules} | {
            s.counterparty_account_id for s in schedules if s.counterparty_account_id
        }
        paths = (
            {
                a.id: a.path
                for a in session.execute(
                    select(Account).where(Account.id.in_(account_ids))
                ).scalars()
            }
            if account_ids
            else {}
        )
        return [
            {
                "id": str(s.id),
                "name": s.name,
                "payee": s.payee.name if s.payee is not None else None,
                "memo": s.memo,
                "account": paths.get(s.account_id),
                "rrule": s.rrule,
                "dtstart": s.dtstart.isoformat(),
                "amount": _amount(s.amount),
                "amount_type": s.amount_type,
                "estimate_basis": s.estimate_basis,
                "counterparty": (
                    paths.get(s.counterparty_account_id) if s.counterparty_account_id else None
                ),
                "category": str(s.category_path) if s.category_path else None,
                "next_due": s.next_due.isoformat() if s.next_due else None,
                "status": s.status,
                "source": s.source,
            }
            for s in schedules
        ]

    return await _run(op)


@audited_tool(
    kind="read",
    entity="match",
    scope="read",
    emits=Emits(payee=True, narrative=True, amount=("[].amount",)),
)
async def list_unmatched(direction: str = "expected") -> list[dict[str, Any]]:
    """Unmatched report with aging. direction=expected -> late bills;
    direction=posted -> unexpected spend to triage. An expected row the
    matcher has proposed a bank row for carries has_proposed_match,
    proposed_posted_id (that bank row's id) and proposal_id (the proposal's
    id); both keys are absent otherwise. The proposal waits in
    list_suggestions (kind='match') for decide_suggestion; match with
    expected_id = transaction_id and posted_id = proposed_posted_id also
    confirms it."""

    def op(session: Session) -> list[dict[str, Any]]:
        from purser import matching as service

        rows = service.list_unmatched(session, direction=direction)
        return [
            {
                "transaction_id": str(r["transaction_id"]),
                "date": r["date"].isoformat(),
                "amount": _amount(r["amount"]),
                "payee": r["payee"],
                "aging_days": r["aging_days"],
                "has_proposed_match": r["has_proposed_match"],
                # Present only when set (guardrail 6).
                **(
                    {
                        "proposed_posted_id": str(r["proposed_posted_id"]),
                        "proposal_id": str(r["proposal_id"]),
                    }
                    if r["proposed_posted_id"] is not None
                    else {}
                ),
            }
            for r in rows[:25]
        ]

    return await _run(op)


@audited_tool(kind="read", entity="docs", scope="read", emits=NOTHING)
async def search_docs(query: str, limit: int = 3) -> dict[str, Any]:
    """Search the Purser documentation (setup, how-to guides, REST/MCP
    reference, concepts) and return the most relevant sections with links
    and short excerpts. For questions about operating or integrating
    Purser — not for ledger data (use describe and the aggregate tools)."""
    from purser.config import get_settings
    from purser.mcp import docsearch

    # Pure CPU over the bundled index — no DB session needed; the
    # audited_tool wrapper still records the read on its own session.
    results = docsearch.search(query, max(1, min(int(limit), 5)), get_settings().docs_url)
    if results is None:
        return {"results": [], "note": "no docs index bundled in this build"}
    if not results:
        return {"results": [], "note": "no matches; try different terms or browse the docs site"}
    return {"results": results}


@audited_tool(kind="read", entity="transaction", scope="read", emits=NOTHING)
async def present(transaction_ids: list[str], note: str | None = None) -> dict[str, Any]:
    """Show the user transactions without passing their details through you.
    Returns a link that opens those rows, in full detail and in the order
    given, for the signed-in user in Purser's web app. Reply with the link
    ("your four largest charges this week", then the link) instead of repeating the
    payees, descriptions or amounts. Up to 50 ids; note (200 characters or
    fewer) is shown above the rows. The link carries only ids and the note."""

    def op(session: Session) -> dict[str, Any]:
        from purser.selection import selection_link

        return selection_link(session, transaction_ids, note)

    return await _run(op)


@audited_tool(
    kind="read", entity="account", scope="read", emits=Emits(amount=("statements[].owed",))
)
async def card_statement_cycle(account: str) -> dict[str, Any]:
    """A credit card's statement cycle (path or id): statement_close_day (the
    day of the month its statement closes, or null), suggested_close_day
    with suggestion_source and suggestion_detail (history: the day whose
    closing balance equals most of the card's full payments; statement: the
    day of the latest recorded statement date; none: too few payments), and
    the latest statements (close date, amount owed at the close) once a
    closing day is set. Use it before setting statement_close_day with
    update_account: confirm the suggestion with the user, or take the day
    from a statement. Card payments sized from the statement cycle read
    these statements."""

    def op(session: Session) -> dict[str, Any]:
        from purser import cycles

        card = ledger.resolve_account(session, account)
        return cycles.describe_card(session, card)

    return await _run(op)
