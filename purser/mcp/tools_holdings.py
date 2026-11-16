"""Holdings tools (issue #390, DESIGN §8/§10, decisions 88/89).

``list_holdings`` answers in three tiers (guardrail 10): a headline text
block that is ALWAYS present — an empty answer included — then a compact
``structuredContent`` fitted to the response budget (guardrail 6). The
write tools are write scope, like ``record_valuation`` (decision 73): a
position has no pending state, so a token without ``write`` gets a tool
error, never a draft. ``record_sale`` (decision 103) goes through the same
service path as REST ``POST /positions/{id}/events`` kind ``sell``, and
``reverse_position_event`` through REST ``POST /position-events/{id}/reverse``
(an agent that recorded a wrong sale undoes it). Registered on import.
"""

import json
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import CallToolResult
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import clock
from purser.mcp import presentation
from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import NOTHING, Emits
from purser.mcp.toolschemas import (
    HoldingsResult,
    LotAllocationInput,
    RecordPositionResult,
    RecordSaleResult,
    ReopenPositionResult,
    ReversePositionEventResult,
)

if TYPE_CHECKING:
    from purser.holdings.service import Sale

# The structuredContent character budget (guardrail 6): 1,900 characters of
# JSON keeps the payload under the < 500-token target the contract tests
# measure as len(json.dumps(payload)) / 4. Every answer is fitted to it.
_HOLDINGS_BUDGET_CHARS = 1900
# Itemized positions per account when every account is listed: the five
# largest. One account named by the caller is paged instead (cursor /
# next_cursor, in symbol order), each page fitted to the same budget.
_TOP_ALL = 5
# Unvested award symbols itemized per account (the rest: list_awards).
_UNVESTED_TOP = 3


def _cents(value: Decimal | None) -> str | None:
    return str(value.quantize(Decimal("0.01"))) if value is not None else None


def _plain(value: Decimal) -> str:
    """A quantity or price without trailing zeros (412.500000 -> 412.5)."""
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


def _decimal(raw: str | None, field: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal string, got {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"{field} must be a finite decimal, got {raw!r}")
    return value


def _fit(
    entries: list[dict[str, Any]],
    candidates: list[tuple[int, dict[str, Any]]],
    *,
    total: str,
    budget: int = _HOLDINGS_BUDGET_CHARS,
) -> dict[str, Any]:
    """The payload with as many itemized positions as fit ``budget``.

    ``entries`` are the account summaries, largest first; ``candidates``
    (account index, position line) every position that may be itemized,
    largest first. Accounts that do not fit even unitemized are dropped from
    the end; then positions are added in order until the first that would
    overflow, so the listed ones are always the largest. ``truncated`` is
    true whenever a held position or an account is not shown."""
    accounts = [dict(e, top=[]) for e in entries]

    def payload(n_accounts: int) -> dict[str, Any]:
        return {
            "accounts": accounts[:n_accounts],
            "totals": {"market_value": total},
            "truncated": False,
        }

    shown = len(accounts)
    while shown > 0 and len(json.dumps(payload(shown))) > budget:
        shown -= 1
    out = payload(shown)
    for index, line in candidates:
        if index >= shown:
            continue
        accounts[index]["top"].append(line)
        if len(json.dumps(out)) > budget:
            accounts[index]["top"].pop()
            break
    listed = sum(len(a["top"]) for a in accounts[:shown])
    held = sum(e["positions"] for e in entries)
    out["truncated"] = shown < len(entries) or listed < held
    return out


def _page(
    entry: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    cursor: str | None,
    total: str,
    budget: int = _HOLDINGS_BUDGET_CHARS,
) -> dict[str, Any]:
    """One page of a named account's positions, in symbol order: as many
    as fit ``budget`` among the symbols after ``cursor`` (keyset paging, so
    a write between two calls — a recount, a new close, the daily job —
    never makes a position repeat or go missing). A page always lists at
    least one position when any remain, so paging always moves.
    ``next_cursor`` is the last symbol listed when more remain, null on the
    last page; ``truncated`` says a later page remains."""
    after = [line for line in lines if cursor is None or line["symbol"] > cursor]
    account = dict(entry, top=[])
    out: dict[str, Any] = {
        "accounts": [account],
        "totals": {"market_value": total},
        "truncated": False,
        "next_cursor": None,
    }

    def fill(n: int) -> int:
        more = n < len(after)
        account["top"] = after[:n]
        out["truncated"] = more
        out["next_cursor"] = after[n - 1]["symbol"] if more and n else None
        return len(json.dumps(out))

    shown = 0
    for n in range(1, len(after) + 1):
        if fill(n) > budget and n > 1:
            break
        shown = n
    fill(shown)
    return out


@audited_tool(kind="read", entity="holding", scope="read", emits=NOTHING)
async def list_holdings(
    account: str | None = None, cursor: str | None = None
) -> Annotated[CallToolResult, HoldingsResult]:
    """Brokerage holdings: each open brokerage account's newest snapshot set
    — what its value adds on top of its cash in net worth — with its source
    (feed: the bank's snapshots; manual: positions recorded by hand and
    priced daily), the set's date and total, how many positions it holds and
    how many of those sit on a stale close (stale_price) or have no price at
    all (no_price: market_value null, adding nothing — never a silent 0), or
    a close at which the value is too large to store (value_out_of_range:
    check the share count; recording a close cannot fix it).
    top itemizes the largest positions (symbol, quantity, market_value; a
    manual one adds price and price_as_of, unrealized_gain when its cost
    basis is known, stale: true when its close is old), fitted to the
    response budget; truncated says some were left out. totals.market_value
    is the sum over every open account, listed or not. A feed position the
    feed sends with no value (0 or none: a plan-only fund no provider
    prices) is valued from a close recorded with record_price under its
    ticker: it shows price, price_as_of and price_source; with no recorded
    close it counts in no_price. One whose feed symbol is no ticker at all
    (a description) counts in unpriceable and is marked unpriceable: no
    close can value it, so never record one for it. in_transit on a manual
    line is a trade's cash that has not landed on the account yet (a sale's
    proceeds, or minus a cash-funded buy's cost), already inside its
    market_value until the cash date; a line with quantity 0 is such cash
    after a sale of every share.

    Pass account (UUID or path) for that account's positions, in symbol
    order, each with its cost_basis (null when unknown), one page at a time:
    a page holds what fits the same budget; next_cursor is the cursor to
    call with for the next page (the last symbol listed), null on the last.
    Paging by symbol never repeats a position, but each page reads the
    newest set when called: a position added meanwhile with a symbol before
    the cursor is not listed, and the daily price job can move later pages
    to a newer set. With account named, totals.market_value is that account's own
    value; a closed account shows its last set with closed: true and totals
    0.00 (net worth no longer counts it). An account with equity awards
    adds unvested: per symbol, the units not yet vested, their value at the
    latest close and the next vest — informational, never inside
    market_value, totals or net worth. Lot detail is list_lots; awards are
    list_awards."""

    def op(session: Session) -> CallToolResult:
        from purser import appsettings
        from purser import ledger as ledger_service
        from purser import networth as service
        from purser.core import holdings as core
        from purser.models import Account

        today = clock.ledger_today()
        stale_days = appsettings.effective().price_stale_days
        if cursor is not None and account is None:
            raise ValueError("cursor pages one account's positions: pass account as well")
        if account is not None:
            target = ledger_service.resolve_account(session, account)
            if target.type != "brokerage":
                raise ledger_service.LedgerError(
                    f"{target.path} is a {target.type} account; holdings belong to brokerage "
                    "accounts"
                )
            accounts = [target]
        else:
            accounts = list(
                session.execute(
                    select(Account).where(Account.type == "brokerage", Account.closed_at.is_(None))
                ).scalars()
            )

        single = account is not None
        entries: list[dict[str, Any]] = []
        lines: list[list[tuple[Decimal, dict[str, Any]]]] = []
        grand = Decimal(0)
        for acct in accounts:
            # latest_holdings already leaves out a manual set's zero rows (the
            # sold-out tombstones); every row it returns counts in net worth,
            # unless the account is closed.
            rows = service.latest_holdings(session, acct.id)
            market = sum(
                (Decimal(h.market_value) for h in rows if h.market_value is not None), Decimal(0)
            )
            closed = acct.closed_at is not None
            if not closed:
                grand += market
            account_lines: list[tuple[Decimal, dict[str, Any]]] = []
            stale_count = no_price = out_of_range = unpriceable = 0
            for h in rows:
                line: dict[str, Any] = {
                    "symbol": h.symbol,
                    "quantity": _plain(Decimal(h.quantity)) if h.quantity is not None else None,
                    "market_value": _cents(h.market_value),
                }
                if h.in_transit:
                    # A trade's cash not on the account yet, inside
                    # market_value until it lands (decision 104).
                    line["in_transit"] = _cents(h.in_transit)
                if h.market_value is None and h.price is not None and h.quantity:
                    # A close, but a value too large to store: the share
                    # count, not the close (core.Valued.out_of_range).
                    line["value_out_of_range"] = True
                    out_of_range += 1
                elif h.unpriceable:
                    # The feed sent no ticker (a description): no recorded
                    # close can value it (decision 92).
                    line["unpriceable"] = True
                    unpriceable += 1
                elif h.market_value is None:
                    no_price += 1
                if h.price is not None and h.price_as_of is not None:
                    line["price"] = _plain(Decimal(h.price))
                    line["price_as_of"] = h.price_as_of.isoformat()
                    if h.price_source is not None:  # a feed row valued from a stored close
                        line["price_source"] = h.price_source
                    if core.stale(h.price_as_of, today, stale_days):
                        line["stale"] = True
                        stale_count += 1
                # The shares' gain: cash in transit is no gain (decision 104).
                shares_value = (
                    Decimal(h.market_value) - Decimal(h.in_transit or 0)
                    if h.market_value is not None
                    else None
                )
                carrier = h.source == "manual" and not h.quantity  # only cash in transit
                gain = None if carrier else core.unrealized_gain(shares_value, h.cost_basis)
                if gain is not None:
                    line["unrealized_gain"] = _cents(gain)
                if single:
                    line["cost_basis"] = _cents(h.cost_basis)
                account_lines.append((Decimal(h.market_value or 0), line))
            account_lines.sort(key=lambda pair: (-pair[0], pair[1]["symbol"]))
            entry: dict[str, Any] = {
                "account": acct.path,
                # Where the figures come from: the rows' source (just
                # switched, the newest set can still be the other one's).
                "source": rows[0].source if rows else acct.holdings_source,
                "as_of": rows[0].as_of.isoformat() if rows else None,
                "market_value": _cents(market),
                "positions": len(rows),
                "stale_price": stale_count,
                "no_price": no_price,
                "value_out_of_range": out_of_range,
                "unpriceable": unpriceable,
            }
            if closed:
                entry["closed"] = True
            # Unvested award units (#393): informational, outside
            # market_value, totals and net worth.
            from purser.holdings import awards as award_service

            unvested = award_service.unvested(session, acct, today=today)
            if len(unvested) > _UNVESTED_TOP:
                # Bounded: the largest few; list_awards pages the rest.
                entry["unvested_more"] = len(unvested) - _UNVESTED_TOP
                unvested = sorted(unvested, key=lambda u: (-u.units, u.symbol))[:_UNVESTED_TOP]
            if unvested:
                entry["unvested"] = [
                    {
                        "symbol": u.symbol,
                        "units": _plain(u.units),
                        "value": _cents(u.value),
                        "next_vest_date": (
                            u.next_vest_date.isoformat() if u.next_vest_date else None
                        ),
                        "next_vest_units": (
                            _plain(u.next_vest_units) if u.next_vest_units is not None else None
                        ),
                        "unscheduled": _plain(u.unscheduled),
                        "complete": u.complete,
                    }
                    for u in unvested
                ]
            entries.append(entry)
            lines.append(account_lines)

        if single:
            # One named account: paged in symbol order, every page inside
            # the budget (guardrail 6).
            (entry,) = entries
            (account_lines,) = lines
            payload: dict[str, Any] = _page(
                entry,
                sorted((line for _value, line in account_lines), key=lambda line: line["symbol"]),
                cursor=cursor,
                total=_cents(grand) or "0.00",
            )
            return presentation.three_tier(
                _headline(entries, grand, True)
                + _page_note(cursor, payload["accounts"][0]["top"], entry["positions"], payload),
                payload,
            )

        order = sorted(
            range(len(entries)),
            key=lambda i: (-Decimal(entries[i]["market_value"]), entries[i]["account"]),
        )
        entries = [entries[i] for i in order]
        lines = [lines[i] for i in order]
        candidates = sorted(
            (
                (index, value, line)
                for index, account_lines in enumerate(lines)
                for value, line in account_lines[:_TOP_ALL]
            ),
            key=lambda item: (-item[1], item[0], item[2]["symbol"]),
        )
        payload = _fit(
            entries,
            [(index, line) for index, _value, line in candidates],
            total=_cents(grand) or "0.00",
        )
        return presentation.three_tier(_headline(entries, grand, False), payload)

    return await _run(op)


def _page_note(
    cursor: str | None, listed: list[dict[str, Any]], held: int, payload: dict[str, Any]
) -> str:
    """Which positions a named account's page lists, when it is not all."""
    next_cursor = payload["next_cursor"]
    if cursor is None and next_cursor is None:
        return ""
    if not listed:
        return f" Nothing after {cursor}: it holds {held} position(s)."
    note = (
        f" This page lists {len(listed)} of {held} positions, {listed[0]['symbol']} to "
        f"{listed[-1]['symbol']} in symbol order"
    )
    if next_cursor is not None:
        return note + f"; call again with cursor {next_cursor!r} for the next page."
    return note + "."


def _headline(entries: list[dict[str, Any]], total: Decimal, single: bool) -> str:
    if not entries:
        return "No open brokerage accounts."
    held = sum(e["positions"] for e in entries)
    if single:
        e = entries[0]
        if e.get("closed"):
            text = (
                f"{e['account']} is closed: its last holdings ({e['market_value']} across "
                f"{e['positions']} position(s), as of {e['as_of']}) no longer count in net worth."
                if e["positions"]
                else f"{e['account']} is closed and holds no positions."
            )
            return text
        if not e["positions"]:
            text = f"{e['account']} holds no positions ({e['source']} holdings)."
        else:
            text = (
                f"{e['account']} holds {_cents(total)} across {e['positions']} position(s) "
                f"({e['source']} holdings, as of {e['as_of']})."
            )
    else:
        text = (
            f"{len(entries)} brokerage account(s) hold {_cents(total)} across {held} position(s)."
        )
    no_price = sum(e["no_price"] for e in entries)
    out_of_range = sum(e["value_out_of_range"] for e in entries)
    stale = sum(e["stale_price"] for e in entries)
    if no_price:
        text += f" {no_price} position(s) have no price yet and add nothing to net worth."
    unpriceable = sum(e["unpriceable"] for e in entries)
    if unpriceable:
        text += (
            f" {unpriceable} feed position(s) carry no ticker a price could be recorded for "
            "(the feed sent a description) and add nothing to net worth."
        )
    if out_of_range:
        text += (
            f" {out_of_range} position(s) are worth more than an amount can hold at their "
            "close and add nothing to net worth: check the share count."
        )
    if stale:
        text += f" {stale} position(s) are valued on a stale close."
    unvested = [u for e in entries for u in e.get("unvested", [])]
    if unvested:
        text += (
            " Unvested award units (not owned, not in net worth): "
            + ", ".join(f"{u['units']} {u['symbol']}" for u in unvested)
            + "."
        )
    return text


@audited_tool(kind="write", entity="position", scope="write", emits=NOTHING)
async def record_position(
    account: str,
    symbol: str,
    quantity: str,
    as_of: str,
    cost_basis: str | None = None,
    name: str | None = None,
    note: str | None = None,
    source_note: str | None = None,
) -> Annotated[CallToolResult, RecordPositionResult]:
    """Record that a manually tracked brokerage account holds quantity
    shares of symbol on as_of (write scope). "I hold 412.5 ACME as of
    2026-10-14" is one call: it opens the position if the account has none
    open in that symbol, then records a set_quantity event. The account must
    be a brokerage account whose holdings_source is manual (an admin sets
    that once). quantity is a decimal string (up to 6 places); as_of is
    YYYY-MM-DD, today or earlier — a backdated call re-values history from
    that date, and is refused if it would leave the position below zero on
    a later date, or if the position would then hold shares on a day a
    CLOSED position in the symbol on that account held them or sold out
    (positions never overlap; a conflict error: start after that day).
    cost_basis (decimal string) is the TOTAL basis of the quantity, for
    unrealized gain; omitted, a re-count of the count held
    just before keeps the basis, a changed count makes it unknown, and 0
    shares have a basis of 0. source_note says where the figure came from,
    e.g. "broker holdings page, 2026-10-14". The answer carries the
    close in effect today and the market value; warning says when the
    position adds nothing to net worth: no price yet (until record_price or
    the daily provider fetch supplies one), or value_out_of_range, a value
    too large to store at its close, where the share count is the likely
    error (recording a close cannot fix it)."""

    def op(session: Session) -> CallToolResult:
        from purser import appsettings
        from purser.holdings import service as holdings_service
        from purser.models import Account

        principal = _principal()
        figure = _decimal(quantity, "quantity")
        if figure is None:
            raise ValueError("quantity is required")
        recorded = holdings_service.record_position(
            session,
            account_ref=account,
            symbol=symbol,
            quantity=figure,
            as_of=date_type.fromisoformat(as_of),
            cost_basis=_decimal(cost_basis, "cost_basis"),
            name=name,
            note=note,
            source_note=source_note,
            token=_token_row(session, principal),
        )
        target = session.get(Account, recorded.position.account_id)
        assert target is not None
        today = clock.ledger_today()
        state = next(
            s
            for s in holdings_service.position_states(
                session,
                target,
                include_closed=True,
                stale_days=appsettings.effective().price_stale_days,
                today=today,
            )
            if s.position.id == recorded.position.id
        )
        price = state.price
        payload: dict[str, Any] = {
            "account": target.path,
            "symbol": recorded.position.symbol,
            "position_id": str(recorded.position.id),
            "event_id": str(recorded.event.id),
            "created": recorded.created,
            "as_of": recorded.event.occurred_on.isoformat(),
            "quantity": _plain(Decimal(recorded.event.quantity)),
            "quantity_today": _plain(state.quantity),
            "price": _plain(price.close) if price is not None else None,
            "price_as_of": price.price_date.isoformat() if price is not None else None,
            "market_value": _cents(state.market_value),
            "stale_price": state.stale_price,
        }
        verb = "Opened" if recorded.created else "Recorded"
        headline = (
            f"{verb} {target.path} {payload['symbol']}: {payload['quantity']} share(s) as of "
            f"{payload['as_of']}"
        )
        if state.value_out_of_range:
            assert price is not None
            payload["value_out_of_range"] = True
            payload["warning"] = (
                f"{payload['quantity_today']} {payload['symbol']} at {_plain(price.close)} is "
                "worth more than an amount can hold, so this position adds nothing to net "
                "worth; check the share count"
            )
            headline += f". {payload['warning']}."
        elif price is None:
            payload["warning"] = (
                f"{payload['symbol']} has no price yet, so this position adds nothing to net "
                "worth; record_price(symbol, date, close) or the daily provider fetch "
                "supplies one"
            )
            headline += f". {payload['warning']}."
        else:
            headline += (
                f"; worth {payload['market_value']} today at {payload['price']} "
                f"({price.source}, {payload['price_as_of']})"
            )
            if state.stale_price:
                headline += ", a stale close"
            headline += "."
        return presentation.three_tier(headline, payload)

    return await _run(op)


@audited_tool(
    kind="write",
    entity="position",
    scope="write",
    emits=Emits(
        amount=(
            "cash.amount",
            "already_recorded.amount",
            "cash_probe_overridden.amount",
            "lots.cost_basis",
            "lots.proceeds",
            "lots.realized_gain",
        )
    ),
)
async def record_sale(
    account: str,
    symbol: str,
    quantity: str,
    price: str,
    trade_date: str,
    settle_date: str | None = None,
    fees: str | None = None,
    proceeds_to: str = "cash",
    transaction_id: str | None = None,
    note: str | None = None,
    dry_run: bool = False,
    force_cash: bool = False,
    lots: list[LotAllocationInput] | None = None,
    lot_method: str | None = None,
) -> Annotated[CallToolResult, RecordSaleResult]:
    """Record a sale of quantity shares of symbol at price on trade_date
    (YYYY-MM-DD, today or earlier) on a manually tracked brokerage account
    (write scope). The net proceeds (quantity x price - fees, rounded to
    the cent) land on the account's cash on trade_date, the day the shares
    leave the holdings (settle_date posts them on a later settlement date
    instead), booked against the system account equity.securities — never
    income. proceeds_to='none' records the sale without cash (shares
    withheld for tax). If the account already shows a posted row of exactly
    that amount around the settlement date (the broker's own "SOLD" cash),
    the call is refused with cash_already_recorded naming that row: when it
    is the sale's cash, call again with transaction_id set to that row's id
    to link it (a linked row must hold exactly the net proceeds, to the
    cent); when it is a different payment of the same amount, call again
    with force_cash=true to post the sale's cash as well (audited). When the
    broker's own row of exactly that cash arrives AFTER the sale, the import
    links it in place of Purser's row by itself: nothing to repair. A sale
    that would leave the position below zero on
    any date is refused, naming the date. dry_run=true answers with what the
    call would do — shares before and after, the cash and its settlement
    date, and already_recorded when the double-count check finds the cash —
    and writes nothing. quantity, price and fees are decimal strings.

    A position that tracks lots (list_lots) needs the lots the sale drew on:
    lots [{lot, quantity}] where lot is a lot_id from list_lots or the
    broker's lot reference, or 'unallocated' for shares outside every lot;
    or lot_method fifo (oldest first), lifo or hifo (highest basis per share
    first). Use the broker's own lot choice when it shows one. A method never
    takes unallocated shares. The answer's lots summarizes the dispositions
    (basis, proceeds and realized gain where known, terms)."""

    def op(session: Session) -> CallToolResult:
        import uuid

        from purser.holdings import service as holdings_service
        from purser.holdings.cash import CashAlreadyRecorded
        from purser.ledger import LedgerError

        principal = _principal()
        figure = _decimal(quantity, "quantity")
        per_share = _decimal(price, "price")
        if figure is None or per_share is None:
            raise ValueError("quantity and price are required")
        try:
            link = uuid.UUID(transaction_id) if transaction_id else None
        except ValueError as exc:
            raise ValueError(f"transaction_id must be a UUID, got {transaction_id!r}") from exc
        allocation = None
        if lots or lot_method:
            from purser.holdings import service as svc

            position = svc.open_position(
                session,
                ledger_service_resolve(session, account).id,
                svc.symbol_of(symbol),
            )
            if position is None:
                raise LedgerError(f"no open {symbol} position on {account}")
            allocation = svc.resolve_allocation(
                session,
                position.id,
                method=lot_method,
                items=_allocation_items(lots),
            )
        try:
            sale = holdings_service.record_sale(
                session,
                account_ref=account,
                symbol=symbol,
                quantity=figure,
                price=per_share,
                trade_date=date_type.fromisoformat(trade_date),
                settle_on=date_type.fromisoformat(settle_date) if settle_date else None,
                fees=_decimal(fees, "fees"),
                proceeds_to=proceeds_to,
                transaction_id=link,
                note=note,
                token=_token_row(session, principal),
                dry_run=dry_run,
                force_cash=force_cash,
                allocation=allocation,
            )
        except CashAlreadyRecorded as exc:
            # The REST 409's code first, so an agent can match on it.
            raise LedgerError(f"cash_already_recorded: {exc}") from exc
        return _sale_answer(sale, sold=figure, trade_date=trade_date)

    return await _run(op)


def ledger_service_resolve(session: Session, ref: str) -> Any:
    from purser import ledger as ledger_service

    return ledger_service.resolve_account(session, ref)


def _allocation_items(
    lots: list[LotAllocationInput] | None,
) -> list[tuple[str | None, bool, Decimal]] | None:
    """MCP allocations as the service takes them: 'unallocated' names the
    shares outside every lot; anything else is a lot id or reference."""
    if not lots:
        return None
    out: list[tuple[str | None, bool, Decimal]] = []
    for item in lots:
        quantity = _decimal(item.get("quantity"), "lots[].quantity")
        if quantity is None:
            raise ValueError("each lots[] entry needs a quantity")
        ref = (item.get("lot") or "").strip()
        if ref.lower() == "unallocated":
            out.append((None, True, quantity))
        else:
            out.append((ref or None, False, quantity))
    return out


def _lots_summary(dispositions: Any) -> dict[str, Any]:
    """A sale's dispositions in one line: totals where every part is known,
    null otherwise (never a partial sum presented as the whole)."""
    allocated = [d for d in dispositions if d.lot_id is not None]

    def total(attr: str) -> str | None:
        values = [getattr(d, attr) for d in allocated]
        if not values or any(v is None for v in values):
            return None
        return str(sum((Decimal(v) for v in values), Decimal(0)))

    return {
        "dispositions": len(allocated),
        "unallocated": _plain(
            sum((Decimal(d.quantity) for d in dispositions if d.lot_id is None), Decimal(0))
        ),
        "terms": sorted({d.term for d in allocated if d.term}),
        "cost_basis": total("cost_basis"),
        "proceeds": total("proceeds"),
        "realized_gain": total("realized_gain"),
    }


def _sale_answer(sale: "Sale", *, sold: Decimal, trade_date: str) -> CallToolResult:
    """record_sale's three tiers: a headline and the compact payload."""
    symbol = sale.position.symbol
    shares = f"{_plain(sale.quantity_before)} -> {_plain(sale.quantity_after)} shares"
    cash: dict[str, Any] | None = None
    if sale.cash_amount is not None and sale.settle_on is not None:
        cash = {
            "amount": _cents(sale.cash_amount),
            "settle_on": sale.settle_on.isoformat(),
            "transaction_id": (
                str(sale.cash_transaction_id) if sale.cash_transaction_id is not None else None
            ),
        }
    payload: dict[str, Any] = {
        "position": {
            "position_id": str(sale.position.id),
            "symbol": symbol,
            "quantity_before": _plain(sale.quantity_before),
            "quantity_after": _plain(sale.quantity_after),
        },
        "cash": cash,
        "linked": sale.linked,
        "dry_run": sale.dry_run,
        "event_id": str(sale.event_id) if sale.event_id is not None else None,
    }
    if sale.dispositions:
        payload["lots"] = _lots_summary(sale.dispositions)
    overridden = sale.cash_probe_overridden
    payload["cash_probe_overridden"] = (
        {
            "transaction_id": str(overridden.id),
            "date": overridden.date.isoformat(),
            "amount": _cents(
                next(
                    Decimal(p.amount)
                    for p in overridden.postings
                    if p.account_id == sale.account.id
                )
            ),
        }
        if overridden is not None
        else None
    )
    forced = (
        f" (force_cash: posted over transaction {overridden.id} of the same amount on "
        f"{overridden.date.isoformat()})"
        if overridden is not None
        else ""
    )
    found = sale.already_recorded
    if sale.dry_run:
        payload["already_recorded"] = (
            {
                "transaction_id": str(found.transaction_id),
                "date": found.date.isoformat(),
                "amount": _cents(found.amount),
            }
            if found is not None
            else None
        )
    what = f"{_plain(sold)} {symbol} on {sale.account.path} ({trade_date})"
    if cash is None:
        verb = "would move" if sale.dry_run else "moves"
        money = f"no cash {verb} (proceeds_to none)"
    elif sale.linked:
        verb = "would be" if sale.dry_run else "is"
        money = (
            f"the account's row of {cash['amount']} on {cash['settle_on']} {verb} the sale's cash"
        )
    else:
        verb = "would land" if sale.dry_run else "lands"
        money = (
            f"{cash['amount']} {verb} on the cash on {cash['settle_on']}, against equity.securities"
        )
    if not sale.dry_run:
        return presentation.three_tier(f"Sold {what}: {shares}; {money}{forced}.", payload)
    headline = f"Dry run, nothing written: selling {what} would take {shares}"
    if found is not None:
        headline += (
            f", but the account already shows this cash (transaction {found.transaction_id} on "
            f"{found.date.isoformat()} for {_cents(found.amount)}), so the call would be "
            "refused: pass transaction_id to link that row if it is the sale's cash, or "
            "force_cash=true if it is a different payment."
        )
    else:
        headline += f"; {money}{forced.replace('posted over', 'would post over')}."
    return presentation.three_tier(headline, payload)


@audited_tool(kind="write", entity="position", scope="write", emits=NOTHING)
async def reverse_position_event(
    event_id: str, note: str | None = None
) -> Annotated[CallToolResult, ReversePositionEventResult]:
    """Reverse a position event (write scope): the way to undo a wrong
    record_sale or record_position (event_id is in their answer). Events
    are never edited; a reversal row makes the event count no more, and a
    trade's cash goes with it (Purser's own cash row is corrected to zero, a
    linked row gets its category back). Then record the right event. Refused
    when the remaining history would leave the position below zero on some
    date, when the event is already reversed or is itself a reversal, and
    when the position is closed: a closed position is reopened first (REST
    PATCH /positions/{id} with closed=false). A trade's cash row in
    review_batch (flag securities_trade) is corrected this way, never with
    update_transaction or confirm_transfer. note says why (optional)."""

    def op(session: Session) -> CallToolResult:
        import uuid

        from purser.holdings import service as holdings_service
        from purser.models import PositionEvent

        principal = _principal()
        try:
            target = uuid.UUID(event_id)
        except ValueError as exc:
            raise ValueError(f"event_id must be a UUID, got {event_id!r}") from exc
        reversal = holdings_service.reverse_event(
            session, event_id=target, note=note, token=_token_row(session, principal)
        )
        original = session.get(PositionEvent, target)
        assert original is not None
        position = holdings_service.get_position(session, reversal.position_id)
        held = holdings_service.quantity_on(session, position, clock.ledger_today())
        cash: dict[str, Any] | None = None
        if original.transaction_id is not None and reversal.transaction_id is not None:
            cash = {
                "transaction_id": str(original.transaction_id),
                "undone_by": str(reversal.transaction_id),
            }
        payload: dict[str, Any] = {
            "reverses_id": str(original.id),
            "reversal_id": str(reversal.id),
            "kind": original.kind,
            "occurred_on": original.occurred_on.isoformat(),
            "position": {
                "position_id": str(position.id),
                "symbol": position.symbol,
                "quantity_after": _plain(held),
            },
            "cash": cash,
        }
        headline = (
            f"Reversed the {original.kind} of {_plain(Decimal(original.quantity))} "
            f"{position.symbol} on {original.occurred_on.isoformat()}: the position now holds "
            f"{_plain(held)} shares"
        )
        if cash is not None:
            headline += (
                f"; its cash (transaction {cash['transaction_id']}) is undone by transaction "
                f"{cash['undone_by']}"
            )
        return presentation.three_tier(headline + ".", payload)

    return await _run(op)


@audited_tool(kind="write", entity="position", scope="write", emits=NOTHING)
async def reopen_position(position_id: str) -> Annotated[CallToolResult, ReopenPositionResult]:
    """Reopen a closed position (write scope), so a wrong event on it — its
    last sale, say — can be reversed with reverse_position_event and recorded
    again; close it again afterwards (REST PATCH /positions/{id} with
    closed=true). position_id is in record_sale's and record_position's
    answers and in the "position is closed" refusal. Refused while another
    position in the symbol is open on the account (one open per symbol),
    and while a later position in it exists. Audited, like the REST
    PATCH /positions/{id} with closed=false it shares a service path with."""

    def op(session: Session) -> CallToolResult:
        import uuid

        from purser.holdings import service as holdings_service
        from purser.models import Account

        principal = _principal()
        try:
            target = uuid.UUID(position_id)
        except ValueError as exc:
            raise ValueError(f"position_id must be a UUID, got {position_id!r}") from exc
        position = holdings_service.update_position(
            session, position_id=target, reopen=True, token=_token_row(session, principal)
        )
        account = session.get(Account, position.account_id)
        assert account is not None
        held = holdings_service.quantity_on(session, position, clock.ledger_today())
        payload = {
            "position_id": str(position.id),
            "symbol": position.symbol,
            "account": account.path,
            "quantity": _plain(held),
        }
        headline = (
            f"Reopened {account.path} {position.symbol} (position {position.id}, holding "
            f"{payload['quantity']}): reverse_position_event its wrong event, record the "
            "right one, then close it again."
        )
        return presentation.three_tier(headline, payload)

    return await _run(op)


@audited_tool(kind="write", entity="price", scope="write", emits=NOTHING)
async def record_price(symbol: str, date: str, close: str) -> dict[str, Any]:
    """Record a symbol's closing price on a date by hand (write scope):
    symbol as the position spells it, date YYYY-MM-DD (today or earlier),
    close a decimal string per share (up to 6 places). It replaces your
    earlier manual close of that date and wins over a provider's close of
    the SAME date; a newer close from any source still supersedes it (prices
    resolve newest date first). Every manually tracked position in the
    symbol is re-valued from that date, and so is a feed position the feed
    values at 0 (a plan-only fund no provider prices: record its unit price
    from the plan's site; weekly is enough, the newest close carries
    forward and turns stale after the staleness threshold)."""

    def op(session: Session) -> dict[str, Any]:
        from purser.holdings import prices as price_service

        principal = _principal()
        figure = _decimal(close, "close")
        if figure is None:
            raise ValueError("close is required")
        recorded = price_service.record_price(
            session,
            symbol=symbol,
            price_date=date_type.fromisoformat(date),
            close=figure,
            token=_token_row(session, principal),
        )
        return {
            "symbol": recorded.symbol,
            "date": recorded.price_date.isoformat(),
            "close": _plain(recorded.close),
            "source": "manual",
            "replaced": _plain(recorded.previous) if recorded.previous is not None else None,
            "accounts_revalued": recorded.accounts,
            "feed_accounts_revalued": recorded.feed_accounts,
        }

    return await _run(op)
