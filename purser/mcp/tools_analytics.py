"""Aggregate read tools — the ones that answer in three tiers.

Split from the compact reads because these carry widget metadata and
``CallToolResult`` envelopes (guardrail 10): headline text,
``structuredContent`` inside the token budget, and a ``purser://``
resource for full-resolution series. Registered on import.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from mcp.types import CallToolResult
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.core import forecasting as core_forecasting
from purser.mcp import presentation
from purser.mcp._registry import _amount, _run, audited_tool
from purser.mcp.privacy import Emits
from purser.mcp.resources import (
    balance_window_segment,
    explicit_balance_window,
    named_balance_window,
    named_net_worth_window,
)
from purser.mcp.toolschemas import (
    BalanceHistoryResult,
    ForecastResult,
    RecurringResult,
    WhatsNewResult,
)


def _payees_for(session: Session, transaction_ids: list[Any]) -> dict[Any, str | None]:
    """Payee names for specific transactions, THROUGH THE ORM — names are
    encrypted at rest and only the type decorator decrypts them (decision
    36), so a raw-SQL join would hand back ciphertext."""
    if not transaction_ids:
        return {}
    from purser.models import Transaction

    rows = session.execute(select(Transaction).where(Transaction.id.in_(transaction_ids))).scalars()
    return {t.id: (t.payee.name if t.payee else None) for t in rows}


@audited_tool(
    kind="read",
    entity="spending",
    scope="read",
    meta=presentation.widget_meta("spending"),
    emits=Emits(payee=True),
)
async def spending_summary(
    group_by: str = "category",
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    period: str | None = None,
    baseline: str | None = None,
) -> CallToolResult:
    """Server-side spending aggregation — no row paging, no client-side
    arithmetic. Classic mode groups by category | payee | month over an
    optional date range. Baseline mode (period='YYYY-MM' + baseline =
    avg_3mo | avg_12mo | same_month_last_year) answers 'where did my money
    go vs my usual?': per-row amount/baseline/delta with top_payee_mover
    attribution; when period is the current month the baseline is
    pace-adjusted to like days elapsed. Drill a category with
    (category=..., group_by='payee', period, baseline) — same signature,
    payee rows. grand_total/baseline_total sum ALL matching spending even
    when rows truncate. Totals are positive-for-spend (category-leg
    convention); search_transactions amounts are negative-for-spend.
    Renders as the spending widget in MCP Apps hosts."""

    def op(session: Session) -> CallToolResult:
        from purser import ledger as ledger_service

        if baseline is not None:
            if period is None:
                raise ValueError("baseline mode needs period='YYYY-MM'")
            payload: dict[str, Any] = ledger_service.spending_baseline(
                session,
                period=period,
                baseline=baseline,
                category_subtree=category,
                group_by="payee" if group_by == "payee" else "category",
            )
            links = presentation.grafana_links("cash-flow")
            if links:
                payload["links"] = links
            delta = Decimal(payload["grand_total"]) - Decimal(payload["baseline_total"])
            sign = "+" if delta >= 0 else ""
            headline = (
                f"{period} spending {payload['grand_total']} is {sign}{_cents(delta)} vs "
                f"{baseline.replace('_', ' ')} ({payload['baseline_total']})"
            )
            mover_row = max(
                (r for r in payload["rows"] if r.get("top_payee_mover")),
                key=lambda r: abs(Decimal(r["delta"])),
                default=None,
            )
            if mover_row and mover_row.get("top_payee_mover"):
                mover = mover_row["top_payee_mover"]
                headline += (
                    f" — biggest mover {mover_row['key']} ({mover_row['delta']}), mostly "
                    f"{mover['payee']} ({mover['count']} vs your usual "
                    f"{mover['baseline_count']})"
                )
            headline += "."
            return presentation.three_tier(headline, payload, widget="spending")

        result = ledger_service.spending_summary(
            session,
            group_by=group_by,
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            category_subtree=category,
        )
        payload = {
            "group_by": group_by,
            "filter": {"start": start, "end": end, "category": category},
            "grand_total": _amount(result["grand_total"]),
            "truncated": result["truncated"],
            "rows": [
                {
                    "key": r["key"],
                    "total": _amount(r["total"]),
                    "transactions": r["transaction_count"],
                }
                for r in result["groups"]
            ],
        }
        scope = category or ("all expenses" if group_by != "payee" else "all payees")
        headline = (
            f"Spending for {scope}"
            f"{f' {start}..{end}' if start or end else ''}: "
            f"{_cents(result['grand_total'])} across {len(payload['rows'])} "
            f"{group_by} group(s)."
        )
        return presentation.three_tier(headline, payload, widget="spending")

    return await _run(op)


def _humanize_bucket(bucket: str) -> str:
    return bucket.replace("_", " ")


@audited_tool(
    kind="read",
    entity="networth",
    scope="read",
    meta=presentation.widget_meta("net-worth"),
    emits=Emits(payee=True),
)
async def net_worth(as_of: str | None = None, period: str | None = None) -> CallToolResult:
    """Net worth as a story, not just a number. Point mode (optional as_of,
    default today): assets, liabilities, per-account breakdown with method
    provenance. Series mode (period = 90d | 1y | all): adaptive-density
    series (weekly/monthly/quarterly — never raw daily; full resolution
    behind series_uri), the change over the period, and attribution — the
    delta decomposed by account class, buckets summing exactly to the
    change. all starts at the earliest ledger activity, at most ten years
    back. Renders as the net-worth widget in MCP Apps hosts. For ONE
    account's history use balance_history."""

    def op(session: Session) -> CallToolResult:
        from purser import networth as service

        today = datetime.now(UTC).date()

        if period is None:
            result = service.net_worth_at(session, date.fromisoformat(as_of) if as_of else today)
            payload: dict[str, Any] = {
                "as_of": result["as_of"].isoformat(),
                "net_worth": _amount(result["net_worth"]),
                "assets": _amount(result["assets"]),
                "liabilities": _amount(result["liabilities"]),
                "breakdown": [
                    {"path": b["path"], "value": _amount(b["value"]), "method": b["method"]}
                    for b in result["breakdown"]
                ],
            }
            headline = (
                f"Net worth {_cents(result['net_worth'])} as of {payload['as_of']} "
                f"(assets {_cents(result['assets'])}, liabilities "
                f"{_cents(result['liabilities'])})."
            )
            return presentation.three_tier(headline, payload, widget="net-worth")

        # The resource resolves its `all` window with this same call, so the
        # series_uri serves at least the window summarized here and, for
        # `all`, exactly it (issue #343).
        start, today = named_net_worth_window(session, period, today=today)
        rows = service.series_sampled(session, start=start, end=today, max_points=20)
        first, current = rows[0], rows[-1]
        delta = current["net_worth"] - first["net_worth"]
        pct = (
            str((delta / first["net_worth"] * 100).quantize(Decimal("0.1")))
            if first["net_worth"] != 0
            else None
        )
        buckets = service.attribution(session, start=start, end=today)
        payload = {
            "current": _cents(current["net_worth"]),
            "change": {"period": period, "delta": _cents(delta), "pct": pct},
            "series": [
                {
                    "date": r["as_of"].isoformat(),
                    "assets": _cents(r["assets"]),
                    "liabilities": _cents(r["liabilities"]),
                    "net": _cents(r["net_worth"]),
                }
                for r in rows
            ],
            "attribution": [{"bucket": b["bucket"], "delta": _cents(b["delta"])} for b in buckets],
            "series_uri": "purser://net-worth/daily",
        }
        links = presentation.grafana_links("net-worth")
        if links:
            payload["links"] = links

        movers = ", ".join(
            f"{_humanize_bucket(b['bucket'])} {'+' if b['delta'] >= 0 else ''}{_cents(b['delta'])}"
            for b in buckets[:3]
        )
        sign = "+" if delta >= 0 else ""
        headline = (
            f"Net worth {payload['current']}, {sign}{_cents(delta)}"
            f"{f' ({sign}{pct}%)' if pct else ''} over {period}"
        )
        if movers:
            headline += f" — {movers}"
        headline += "."
        return presentation.three_tier(headline, payload, widget="net-worth")

    return await _run(op)


# The character budget the forecast payload is fitted to (guardrail 6, #354):
# 1,900 characters of JSON keeps it under the < 500-token target the contract
# tests measure as len(json.dumps(payload)) / 4, with room to spare.
_FORECAST_BUDGET_CHARS = 1900

# The bases an MCP forecast event names (decision 80): amounts a user or an
# agent supplied. An estimate's basis is the schedule's, one list_schedules
# away, and every key costs the fitted events; REST and the resource always
# carry basis.
_SUPPLIED_BASES = frozenset({"statement", "override"})


def _cents(value: Decimal | None) -> str | None:
    """Presentation-tier money: cents precision (ledger stores NUMERIC(20,4);
    the extra places are always zero for USD and just burn token budget)."""
    return str(value.quantize(Decimal("0.01"))) if value is not None else None


def _fit_events(
    events: list[dict[str, Any]],
    *,
    low_index: int | None,
    materiality: Decimal | None,
    payload: dict[str, Any],
    budget: int = _FORECAST_BUDGET_CHARS,
) -> dict[str, Any]:
    """``payload`` with the chronological ``events`` fitted to ``budget``
    characters of JSON (#354, decision 77). It replaces a fixed count cap,
    which hid mid-size moves whenever ten larger ones were in range and could
    not see how long the names were.

    An event qualifies when |amount| >= ``materiality`` (every event when
    None). The event that produces the low (``low_index``) comes first,
    whatever it weighs: the headline names it. Qualifying events follow by
    |amount| descending, ties by date. They are added in that order while
    the serialized payload, ``omitted`` included, stays within ``budget``;
    the first that would overflow ends the fit, so the listed events are
    always the largest qualifying ones. They are presented in date order.

    ``truncated`` is true only when the budget left out a qualifying event.
    ``omitted`` ({count, net}) is present whenever any event is not listed,
    for either reason, so start_balance + Σ events.amount + omitted.net is
    end_balance."""
    amounts = [Decimal(e["amount"]) for e in events]

    def qualifies(i: int) -> bool:
        return materiality is None or abs(amounts[i]) >= materiality

    def build(kept: set[int]) -> dict[str, Any]:
        out = dict(payload)
        out["events"] = [events[i] for i in sorted(kept)]
        dropped = [i for i in range(len(events)) if i not in kept]
        out["truncated"] = any(qualifies(i) for i in dropped)
        if dropped:
            net = sum((amounts[i] for i in dropped), Decimal(0))
            out["omitted"] = {"count": len(dropped), "net": _cents(net)}
        return out

    ranked = sorted(
        (i for i in range(len(events)) if i == low_index or qualifies(i)),
        key=lambda i: (i != low_index, -abs(amounts[i]), events[i]["date"], i),
    )
    kept: set[int] = set()
    fitted = build(kept)
    for i in ranked:
        trial = build(kept | {i})
        if i != low_index and len(json.dumps(trial)) > budget:
            break
        kept.add(i)
        fitted = trial
    return fitted


def _band_driver(
    fc: core_forecasting.Forecast, *, until: date
) -> core_forecasting.ForecastEvent | None:
    """The ranged event on or before ``until`` whose band costs the most
    against its point (#247): an outflow coming in high, an inflow low."""
    ranged = [
        event
        for day in fc.days
        if day.date <= until
        for event in day.events
        if event.range is not None
    ]
    return max(ranged, key=lambda e: e.amount - (e.range or (e.amount, e.amount))[0], default=None)


def _materiality(raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"materiality must be a decimal string, got {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"materiality must be a non-negative amount, got {raw!r}")
    return value


@audited_tool(
    kind="read",
    entity="forecast",
    scope="read",
    meta=presentation.widget_meta("forecast"),
    emits=Emits(
        payee=True,
        narrative=True,
        description=("events[].name",),
        amount=("events[].amount", "events[].range"),
        balance=("events[].balance_after",),
    ),
)
async def forecast_balance(
    account: str, days: int = 30, materiality: str | None = None
) -> Annotated[CallToolResult, ForecastResult]:
    """Deterministic balance projection with every move explained. The
    events array IS the chart: each entry carries the running balance_after,
    so the step line and the narration come from the same data. low is the
    projected minimum and end_balance the balance on the last day;
    min_balance is the account floor (null when unset). coverage says what
    the line is made of — schedules_active == 0 means a flat line by
    construction, not a quiet month, and suggestions_open how many candidate
    schedules await a decision (list_suggestions), and matches_pending how
    many instances are left out as already paid: an open match proposal
    names a bank row that is in the balance (matches_pending_net, their net,
    only when nonzero; list_unmatched names each one's proposed_posted_id
    and match on that pair confirms it). Events flagged projected are rule
    occurrences past the materialized horizon (no instance row yet);
    overdue ones are still due and land today. An event with basis
    'statement' carries a card statement balance someone recorded
    (record_statement_balance) and one with 'override' an amount the user
    set; without basis the amount is the schedule's own (fixed or an
    estimate — a card payment's default is the card's current balance,
    not its statement). An estimated event may carry range {low, high}: the
    min and max of its schedule's newest six settled amounts and its own;
    low.range {low, high, low_date} is the projected low with every ranged
    event at its worst and at its best, and the headline names what could
    take the account under its floor. days is 1..365. events lists
    the largest movements that fit the response budget, always including
    the one that makes the low; materiality (a decimal string) lists only
    movements at least that large. omitted {count, net} accounts for every
    movement not listed, so start_balance + the listed amounts +
    omitted.net = end_balance; truncated is true when the budget, not
    materiality, left one out. daily_uri is the full daily series:
    resources/read, or read_series where only tools are forwarded. Renders
    as the forecast widget in MCP Apps hosts. Looking BACKWARD is
    balance_history(account, period)."""

    def op(session: Session) -> CallToolResult:
        from purser import schedules as service

        horizon = service.validate_horizon(days)
        threshold = _materiality(materiality)
        result = service.forecast_account(session, account_ref=account, horizon_days=horizon)
        fc = result["forecast"]
        path = result["account_path"]

        running = fc.start_balance
        events: list[dict[str, Any]] = []
        for day in fc.days:
            for event in day.events:
                running += event.amount
                events.append(
                    {
                        "date": day.date.isoformat(),
                        "name": event.label,
                        "amount": _cents(event.amount),
                        "balance_after": _cents(running),
                        "transaction_id": (
                            str(event.transaction_id) if event.transaction_id else None
                        ),
                        # Flags only when set — every key costs the fitted
                        # payload events (guardrail 6, #235).
                        **({"projected": True} if event.projected else {}),
                        **({"overdue": True} if event.overdue else {}),
                        # Only the amounts someone supplied (decision 80): the
                        # agent must be able to say where they came from.
                        **({"basis": event.basis} if event.basis in _SUPPLIED_BASES else {}),
                        **(
                            {
                                "range": {
                                    "low": _cents(event.range[0]),
                                    "high": _cents(event.range[1]),
                                }
                            }
                            if event.range is not None
                            else {}
                        ),
                    }
                )

        minimum = min(fc.days, key=lambda d: d.balance)
        low_date = minimum.date.isoformat()
        # The event that produces the low: the last one on the low's day (a
        # low on day 0 with nothing landing there is the opening balance).
        low_index = next(
            (i for i in range(len(events) - 1, -1, -1) if events[i]["date"] == low_date), None
        )
        floor = fc.min_balance if fc.min_balance is not None else Decimal(0)
        coverage = dict(result["coverage"])
        if "matches_pending_net" in coverage:
            coverage["matches_pending_net"] = _cents(coverage["matches_pending_net"])
        base: dict[str, Any] = {
            "account": path,
            "as_of": fc.days[0].date.isoformat(),
            "horizon_days": horizon,
            "start_balance": _cents(fc.start_balance),
            "end_balance": _cents(fc.days[-1].balance),
            "min_balance": _cents(fc.min_balance),
            "low": {"date": low_date, "balance": _cents(minimum.balance)},
            "coverage": coverage,
            "events": [],
            "truncated": False,
            "daily_uri": f"purser://forecast/{path}/{horizon}d",
        }
        pessimistic, optimistic = fc.pessimistic_low, fc.optimistic_low
        if pessimistic is not None and optimistic is not None:
            # The estimate range band under the low (#247, decision 85).
            base["low"]["range"] = {
                "low": _cents(pessimistic.balance),
                "high": _cents(optimistic.balance),
                "low_date": pessimistic.date.isoformat(),
            }
        links = presentation.grafana_links("upcoming-bills")
        if links:
            base["links"] = links
        payload = _fit_events(events, low_index=low_index, materiality=threshold, payload=base)

        causal = events[low_index] if low_index is not None else None
        notes = ""
        if coverage["matches_pending"]:
            net = coverage.get("matches_pending_net")
            notes = (
                f" {coverage['matches_pending']} instance(s)"
                f"{f' (net {net})' if net else ''} are left out as paid, pending "
                "confirmation of a proposed match (list_unmatched gives each one's "
                "proposed_posted_id; match confirms it)."
            )
        omitted = payload.get("omitted")
        if payload["truncated"] and omitted:
            notes += (
                f" {omitted['count']} more movement(s) (net {omitted['net']}) are not "
                "itemized; read_series(daily_uri) has every day."
            )
        if coverage["schedules_active"] == 0 and not events:
            # The chat's complaint (#235): "stays above its floor" with zero
            # events read as a calm month. Say what the line is.
            headline = (
                f"No schedules are modeled on {path}, so this projection is flat by "
                f"construction — the balance stays at {payload['start_balance']} for "
                f"{horizon} days only because nothing is expected."
            )
            if coverage["suggestions_open"]:
                headline += (
                    f" {coverage['suggestions_open']} suggestion(s) for this account await a "
                    "decision (list_suggestions)."
                )
            return presentation.three_tier(headline + notes, payload, widget="forecast")
        if minimum.balance < floor:
            headline = (
                f"{path} is projected to hit {payload['low']['balance']} "
                f"on {payload['low']['date']}"
            )
            if causal:
                headline += f" — {causal['name']} ({causal['amount']}) lands that day"
            headline += f"; the account floor is {floor}."
        else:
            headline = (
                f"{path} stays above its {floor} floor for the next {horizon} days; "
                f"projected low {payload['low']['balance']} on {payload['low']['date']}."
            )
            if pessimistic is not None and pessimistic.balance < floor:
                # The band straddles the floor: name what would take it under.
                driver = _band_driver(fc, until=pessimistic.date)
                headline += (
                    f" It could dip to {_cents(pessimistic.balance)} on "
                    f"{pessimistic.date.isoformat()}"
                )
                if driver is not None:
                    headline += (
                        f" if {driver.label} comes in {'high' if driver.amount < 0 else 'low'}"
                    )
                headline += "."
        headline += (
            f" Modeled from {coverage['schedules_active']} schedule(s): "
            f"{coverage['instances']} instance(s), {coverage['projected']} projected."
        )
        return presentation.three_tier(headline + notes, payload, widget="forecast")

    return await _run(op)


@audited_tool(
    kind="read",
    entity="schedule",
    scope="read",
    meta=presentation.widget_meta("recurring"),
    emits=Emits(
        payee=True,
        narrative=True,
        amount=("items[].amount", "items[].monthly_equiv", "items[].last_change"),
    ),
)
async def list_recurring() -> Annotated[CallToolResult, RecurringResult]:
    """What am I paying every month, and what changed? Every active outflow
    schedule with an honest monthly_equiv (annual/semi-annual bills
    normalized), next due date, and last_change derived from confirmed-match
    price history. monthly_total/annualized_total/changes_12mo are computed
    server-side over ALL schedules — never sum rows yourself. items lists
    the largest monthly costs (truncated flag beyond); source is provenance
    only (manual, detected, agent), never a status. Only active schedules
    are listed; patterns awaiting a decision live in the suggestions inbox
    — suggestions_open counts them (list_suggestions kind=schedule).
    Renders as the recurring-radar widget in MCP Apps hosts."""

    def op(session: Session) -> CallToolResult:
        from purser import schedules as service

        payload = service.list_recurring(session)
        changes = payload["changes_12mo"]
        headline = (
            f"{payload['count']} recurring bills ≈ ${payload['monthly_total']}/mo "
            f"(${payload['annualized_total']}/yr)"
        )
        if changes["count"]:
            sign = "+" if not str(changes["delta"]).startswith("-") else ""
            headline += (
                f"; {changes['count']} price change(s) in 12mo ({sign}${changes['delta']}/mo)"
            )
        headline += "."
        return presentation.three_tier(headline, payload, widget="recurring")

    return await _run(op)


@audited_tool(
    kind="read",
    entity="system",
    scope="read",
    meta=presentation.widget_meta("digest"),
    emits=Emits(
        payee=True,
        narrative=True,
        description=(
            "transactions.notable[].description",
            "pendings_vanished.notable[].description",
            "matches[].name",
            "upcoming.items[].name",
        ),
        amount=(
            "transactions.notable[].amount",
            "pendings_vanished.notable[].amount",
            "matches[].amount",
            "matches[].variance",
            "upcoming.items[].amount",
            "price_changes[].delta",
        ),
    ),
)
async def whats_new(
    since_cursor: str | None = None, materiality: str | None = None
) -> Annotated[CallToolResult, WhatsNewResult]:
    """The morning brief: everything material since your last call in one
    compact payload — new transactions (count/total, notable items above the
    materiality threshold, default $50), confirmed bill matches (variance
    breaches always itemized), subscription price changes, projected
    shortfalls, bank-reported balance drift against the ledger (each line
    names its basis: posted_only when that bank's balance leaves pending
    charges out, else pending_inclusive; pinned: true when an admin pinned
    that basis instead of it being learned), pending transactions the bank dropped
    without posting (counted; those above the threshold itemized — a dropped
    refund is money to chase), manually tracked symbols whose price turned
    stale or that have none (stale_prices; record_price fixes one), feed
    positions the feed sends with no value that no recorded close prices
    (unpriced_holdings; record_price under the ticker values one, except
    one marked unpriceable: its feed symbol is no ticker, so no close can
    value it), brokerage accounts switched back to the feed whose
    hand-tracked set still counts because no fetch has arrived since
    (open_manual_periods: check the connection or the account mapping),
    the week's expected bills, and sync failures.
    Pass the returned cursor on your next call to get only new material; a
    first call without a cursor looks back 7 days. Every line carries its
    entity id so you can drill without a search. Narrate this in your own
    voice; the digest widget is the visual receipt."""

    def op(session: Session) -> CallToolResult:
        from purser import digest as digest_service

        payload: dict[str, Any] = digest_service.whats_new(
            session,
            since_cursor=since_cursor,
            materiality=(
                Decimal(materiality) if materiality else digest_service.DEFAULT_MATERIALITY
            ),
        )
        parts: list[str] = []
        tx = payload["transactions"]
        if tx["count"]:
            parts.append(f"{tx['count']} new transaction(s) totaling ${tx['total']}")
        if payload["matches"]:
            parts.append(f"{len(payload['matches'])} bill(s) matched")
        for change in payload["price_changes"]:
            parts.append(f"{change['payee']} price change {change['delta']}")
        for flag in payload["forecast_flags"]:
            low = f" (low {flag['low']})" if flag.get("low") else ""
            parts.append(f"{flag['account']} projected shortfall on {flag['date']}{low}")
        for drifted in payload["balance_drifts"]:
            basis = drifted.get("basis")
            pin = ", pinned" if drifted.get("pinned") else ""
            on = f" ({basis.replace('_', '-')} basis{pin})" if basis else ""
            parts.append(f"{drifted['account']} balance drift {drifted['delta']} vs bank{on}")
        vanished = payload["pendings_vanished"]
        if vanished["count"]:
            parts.append(f"{vanished['count']} pending transaction(s) dropped without posting")
        for stale in payload["stale_prices"]:
            since = stale.get("price_as_of")
            parts.append(
                f"{stale['symbol']} price stale since {since}"
                if since
                else f"{stale['symbol']} has no price"
            )
        for unpriced in payload["unpriced_holdings"]:
            parts.append(
                f"{unpriced['account']} {unpriced['symbol']} has no value and no ticker a price "
                "could be recorded for (the feed sent a description)"
                if unpriced.get("unpriceable")
                else f"{unpriced['account']} {unpriced['symbol']} has no price (the feed sends no "
                "value for it)"
            )
        for period in payload["open_manual_periods"]:
            parts.append(
                f"{period['account']} still shows its hand-tracked holdings: no feed fetch "
                f"since the switch back on {period['since']}"
            )
        upcoming = payload["upcoming"]
        if upcoming["count"]:
            parts.append(f"{upcoming['count']} bill(s) ≈ ${upcoming['total']} due this week")
        if payload["sync"]["failures"]:
            parts.append(f"{payload['sync']['failures']} sync failure(s)")
        review_block = payload.get("review") or {}
        if review_block.get("unreviewed"):
            aged = review_block.get("oldest_days")
            stale = f", oldest {aged}d" if aged else ""
            parts.append(f"{review_block['unreviewed']} transaction(s) awaiting review{stale}")
        headline = ("; ".join(parts) + ".") if parts else "Quiet since your last check."
        return presentation.three_tier(headline, payload, widget="digest")

    return await _run(op)


_BALANCE_MOVER_CAP = 2


@audited_tool(
    kind="read",
    entity="account",
    scope="read",
    meta=presentation.widget_meta("balance-history"),
    emits=Emits(payee=True, amount=("movers[].amount",)),
)
async def balance_history(
    account: str,
    period: str = "90d",
    start: str | None = None,
    end: str | None = None,
) -> Annotated[CallToolResult, BalanceHistoryResult]:
    """One account's balance over time, in a single call. period = 30d | 90d
    | 1y | all (or pass start and end for an explicit window, which wins).
    Adaptive density chosen server-side and reported in `density` — 30d is a
    true daily series. `low`/`high` are the lowest and highest END-OF-DAY
    balance over EVERY day with activity in the window, not just the returned
    points, so they stay true under downsampling, and the order of one day's
    rows (pay posted before its deductions) never creates one; `movers` names
    the biggest balance movements. Full per-transaction resolution is behind
    series_uri. This is the historical counterpart to forecast_balance; for
    the whole portfolio use net_worth.
    Never reconstruct a balance series by walking transactions."""

    def op(session: Session) -> CallToolResult:
        from purser import balances as balance_service
        from purser import ledger as ledger_service
        from purser import networth as networth_service
        from purser.core import sampling

        target = ledger_service.resolve_account(session, account)
        if target.is_virtual:
            raise ledger_service.LedgerError(
                f"{target.path} is a category; balances apply to real accounts"
            )
        # The resource resolves its period segment with these same calls, so
        # the series_uri below always resolves (issue #289) and serves the
        # very window summarized here (issue #322).
        if start is not None and end is not None:
            window_start, window_end = explicit_balance_window(start, end)
            label = f"{window_start.isoformat()}..{window_end.isoformat()}"
            segment = balance_window_segment(window_start, window_end)
        else:
            window_start, window_end = named_balance_window(
                session, period, today=datetime.now(UTC).date()
            )
            label = segment = period

        dates = sampling.sample_dates(
            window_start,
            window_end,
            intervals=balance_service.BALANCE_INTERVALS_DAYS,
            max_points=balance_service.BALANCE_MAX_POINTS,
        )
        series, method = networth_service.account_balance_series(session, target, dates=dates)
        points = {
            "dates": [d.isoformat() for d in dates],
            "balances": [_cents(series[d]) for d in dates],
        }
        opening, current = series[dates[0]], series[dates[-1]]
        delta = current - opening
        pct = str((delta / abs(opening) * 100).quantize(Decimal("0.1"))) if opening != 0 else None

        low, high = balance_service.window_extremes(
            session, target.id, start=window_start, end=window_end
        )
        rows, _truncated = balance_service.change_points(
            session, target.id, start=window_start, end=window_end
        )
        movers = balance_service.largest_moves(
            session,
            target.id,
            start=window_start,
            end=window_end,
            limit=_BALANCE_MOVER_CAP,
        )
        # Payee names are encrypted at rest and only the ORM's type decorator
        # decrypts them (decision 36) — resolve through the ORM, never by
        # joining payees in the raw SQL above.
        payees = _payees_for(session, [m.transaction_id for m in movers])

        payload: dict[str, Any] = {
            "account": target.path,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "currency": target.currency.strip(),
            "method": method,
            "current": _cents(current),
            "change": {"period": label, "delta": _cents(delta), "pct": pct},
            "low": (
                {"date": low.date.isoformat(), "balance": _cents(Decimal(low.balance))}
                if low is not None
                else None
            ),
            "high": (
                {"date": high.date.isoformat(), "balance": _cents(Decimal(high.balance))}
                if high is not None
                else None
            ),
            "density": {
                "interval_days": (dates[-1] - dates[-2]).days if len(dates) > 1 else 0,
                "points": len(dates),
            },
            "transactions": len(rows),
            "series": points,
            "movers": [
                {
                    "date": m.date.isoformat(),
                    "payee": payees.get(m.transaction_id),
                    "amount": _cents(Decimal(m.delta)),
                    "transaction_id": str(m.transaction_id),
                }
                for m in movers
            ],
            "series_uri": f"purser://balance-history/{target.path}/{segment}",
        }

        sign = "+" if delta >= 0 else ""
        headline = (
            f"{target.path} is {payload['current']}, {sign}{_cents(delta)}"
            f"{f' ({sign}{pct}%)' if pct else ''} over {label}"
        )
        if low is not None and Decimal(low.balance) != current:
            headline += f" — low {_cents(Decimal(low.balance))} on {low.date.isoformat()}"
        if movers:
            top = movers[0]
            name = payees.get(top.transaction_id) or "a transaction"
            headline += (
                f"{';' if low is not None else ' —'} biggest move {name} "
                f"{_cents(Decimal(top.delta))} on {top.date.isoformat()}"
            )
        return presentation.three_tier(headline + ".", payload, widget="balance-history")

    return await _run(op)
