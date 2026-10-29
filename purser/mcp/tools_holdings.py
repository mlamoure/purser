"""Holdings tools (issue #390, DESIGN §8/§10, decisions 88/89).

``list_holdings`` answers in three tiers (guardrail 10): a headline text
block that is ALWAYS present — an empty answer included — then a compact
``structuredContent`` fitted to the response budget (guardrail 6). The two
write tools are write scope, like ``record_valuation`` (decision 73): a
position has no pending state, so a token without ``write`` gets a tool
error, never a draft. Registered on import.
"""

import json
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from mcp.types import CallToolResult
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.mcp import presentation
from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import NOTHING
from purser.mcp.toolschemas import HoldingsResult, RecordPositionResult

# The structuredContent character budget (guardrail 6): 1,900 characters of
# JSON keeps the payload under the < 500-token target the contract tests
# measure as len(json.dumps(payload)) / 4. Every answer is fitted to it.
_HOLDINGS_BUDGET_CHARS = 1900
# Itemized positions per account when every account is listed: the five
# largest. One account named by the caller is paged instead (cursor /
# next_cursor, in symbol order), each page fitted to the same budget.
_TOP_ALL = 5


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
    close can value it, so never record one for it.

    Pass account (UUID or path) for that account's positions, in symbol
    order, each with its cost_basis (null when unknown), one page at a time:
    a page holds what fits the same budget; next_cursor is the cursor to
    call with for the next page (the last symbol listed), null on the last.
    Paging by symbol never repeats a position, but each page reads the
    newest set when called: a position added meanwhile with a symbol before
    the cursor is not listed, and the daily price job can move later pages
    to a newer set. With account named, totals.market_value is that account's own
    value; a closed account shows its last set with closed: true and totals
    0.00 (net worth no longer counts it)."""

    def op(session: Session) -> CallToolResult:
        from purser import appsettings
        from purser import ledger as ledger_service
        from purser import networth as service
        from purser.core import holdings as core
        from purser.models import Account

        today = datetime.now(UTC).date()
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
                gain = core.unrealized_gain(h.market_value, h.cost_basis)
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
        from purser.holdings import projection
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
        today = projection.utc_today()
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
