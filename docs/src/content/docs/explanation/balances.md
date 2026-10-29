---
title: Balances
description: Why a balance is recorded rather than recomputed, the three methods that produce one, and why you should never derive a balance by summing rows.
---

A balance in Purser is **recorded, not recomputed**. Every surface that shows
one — the register's Balance column, the account headline, the REST endpoints,
the agent tools — reads a stored number rather than adding up transactions on
demand. This page explains what is stored, the three ways a balance is
produced, and the one rule that matters if you are an agent.

## The rule

**Never derive a balance by summing rows.**

Purser already knows the answer, and summing a page of transactions cannot
reproduce it: a page is a window, corrections supersede rows rather than
editing them, and voided rows still appear in history. Ask for the balance you
want instead.

## Running balances are stored per row

Purser keeps a running balance for every (account, transaction) pair where
that transaction has a posting on that account. The number is the account's
balance immediately after that transaction, accumulated in a fixed total order
so it never depends on how a query happened to sort.

That order is `(date, created_at, transaction_id)`, ascending for accumulation
and exactly reversed in the register. The transaction id is the final tiebreak
because every row imported in one database transaction can share the same date
and creation timestamp. Stored running balances and display order use the same
canonical key, so same-day balances stay stable.

It is derived data — rebuildable from postings at any time, never authoritative
over them — but it is maintained *transactionally*: the recompute happens
inside the same database transaction as the write that invalidated it. A reader
can never observe a stale register. A nightly job re-checks the whole store
against the postings and rebuilds if anything disagrees.

Two consequences worth knowing:

- A balance exists only for **chain-head** rows that are `pending` or `posted`.
  A voided row, a superseded correction, or an `expected` row has no running
  balance, and surfaces render it blank rather than repeating the previous row.
- The same transaction has a **different** running balance in each account it
  touches. A transfer has two. This is why a balance is only meaningful from a
  named account's perspective.

## The three methods

What a surface reports as an account's balance depends on the account type, and
every response says which method produced it via `balance_method` (`method` on
`GET /accounts/{id}/balance`).

| Method | Applies to | What it means |
| --- | --- | --- |
| `postings` | checking, savings, cash, credit cards | The stored running balance alone. |
| `valuation` | loan, property | The latest valuation **replaces** the postings sum. A mortgage's feed reports a balance, not a transaction history, so the postings sum is not the debt. |
| `postings+holdings` | brokerage | The latest holdings market value is **added to** the postings sum. Brokerage cash is real money the feed already reported, so it is not discarded. The holdings come from the feed's snapshots or, for an account [tracked by hand](../../how-to/manual-holdings/), from its positions priced at the newest close. |

When an overlay produced the answer, `balance_as_of` names the date of the
valuation or snapshot used, and `postings_balance` carries the cash-only figure
alongside. Closed accounts always answer from postings: closing zeroes the
account, and a stale valuation or snapshot must not resurrect it.

Net worth uses these same rules, so an account page and `net_worth` agree.

## Two bases, deliberately different

There are two ways to ask "what is the balance", and they differ on
future-dated rows:

- **As of a date** — `GET /accounts/{id}/balance?as_of=`, MCP `account_balance`.
  Reads the stored running balance at that date and stops there. A row dated
  after `as_of` is not counted.
- **Right now** — the accounts list, the account headline, MCP
  `list_accounts`. Includes every `pending` and `posted` row regardless of
  date, because that is what your bank's "current balance" means.

Both apply the same overlays. The difference only shows up when the ledger
holds a future-dated row — a pending charge that has not settled, or a
scheduled entry you posted early. Forecasts start from the **right now** basis
for exactly this reason: a future-dated pending row must not vanish from a
projection.

## Balance over time

Because a running balance is stored per row, a balance history is a read
rather than a computation. `GET /accounts/{id}/balance/series?start=&end=`
returns one point per transaction that moved the account, each carrying the
balance after it, the delta, and the transaction id to drill into — plus the
`opening` balance carried into the window, so `opening` plus every `delta`
equals the last point.

The window defaults to the last 90 days. Points are oldest-first. Past 5000
points the response sets `truncated` rather than quietly dropping the tail.

The same three methods apply, and the account type decides once for the whole
window — an account does not change how it is valued partway through. The
close cutoff, though, applies per date: history before a close keeps its
overlay, and from the close date onward the account reads from postings.

## Reconciled balances

Reconciling an account records a statement balance for a date and posts one
adjustment for any difference. It compares against the same chain-head rows the
running balance uses, on a basis you choose: posted rows only, or posted plus
pending. The claim keeps its target, so every read of the account re-checks it
and reports a `difference` when a later write moved that date's balance. See
[Reconcile an account](../../how-to/reconcile-an-account/).

## Recording a valuation

A loan or property balance is its latest valuation. The feed records one for a
synced account; record one yourself with `POST /accounts/{id}/valuations`, or
from an agent with the MCP tool `record_valuation` (both write scope), giving
the date and the value on that date.

Only loan and property accounts take a valuation. Any other account is refused
with a `422` that says where its balance comes from instead: its transactions,
plus its latest holdings snapshot for a brokerage account. To bring a checking,
savings, cash or credit-card balance to a statement, see
[Reconcile an account](../../how-to/reconcile-an-account/).

`value` is in the ledger's sign by default, the way Purser stores it: positive
for something you own, negative for money you owe. To enter a loan figure the
way its statement prints it, add `"convention": "statement"`, and Purser
negates it exactly as reconciliation does:

| Account | On the date | You send | Purser stores |
| --- | --- | --- | --- |
| Property | worth 400000.00 | `"value": "400000.00"` | `400000.0000` |
| Loan | 250000.00 owed | `"value": "-250000.00"` | `-250000.0000` |
| Loan | 250000.00 owed | `"value": "250000.00", "convention": "statement"` | `-250000.0000` |
| Loan | 250000.00 owed | `"value": "250000.00"` | rejected with `422` |

A positive `value` on a loan is rejected unless you say it is the statement's
figure: recorded as given, it would turn the debt into an asset in
net worth. The answer's `value` is always the stored figure, in ledger sign.

## For agents

Ask `search_transactions` with an `account` and each row carries `balance` —
the same number the register shows, from that account's perspective. Without an
`account` there is no perspective, so there is no `balance` field at all; with a
category there is none either, since categories are virtual and hold no running
balances.

For a single point in time use `account_balance`. For a balance over time use
`balance_history(account, period)` — one call, not a loop over dates. For a
total across rows use `spending_summary`. In no case should you add up
`amount` fields yourself.

`balance_history` picks the density itself and tells you what it picked, so a
naive call cannot blow your context: `30d` is a true daily series, `90d` lands
on every third day, `1y` on every fourteenth. `low` and `high` are computed
over **every** day with activity in the window rather than the returned points,
so they stay true even when the series is sparse — a low on the 19th is
reported correctly by a series that never contains the 19th. `movers` names
the biggest movements. Full per-transaction resolution is behind `series_uri`,
which serves exactly the window the tool summarized (for `all`, the whole
history): up to 5000 points, oldest first, with `truncated` set past that.

`low` and `high` are **end-of-day** balances: each day counts with the balance
it closed at, never with a value it passed through on the way. Purser orders
one day's rows by when they were recorded, not by when the bank processed them,
so that order never produces an extreme. A paycheck imported as a gross pay
line followed by its same-day tax and benefit deductions is not a high, and a
duplicate charge reversed the same day is not a low. The per-transaction points
behind `series_uri` still show those intra-day steps.

The three questions and their tools:

| Question | Tool |
| --- | --- |
| One account, over time | `balance_history(account, period)` |
| Everything, over time | `net_worth(period)` |
| One account, looking forward | `forecast_balance(account, days)` |

`net_worth(period="all")` starts at the ledger's earliest activity but at most
ten years back, and its `series_uri` serves that same window day by day.
