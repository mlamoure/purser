---
title: Forecasting
description: Why Purser forecasts deterministically from schedules and running balances, with no statistical models, and how shortfalls surface.
---

Purser's forecast answers "will this account dip below zero before payday?" and
it answers it the same way every time. The forecast is **deterministic**: it is
your current balance plus the expected transactions your schedules already imply
-- not a prediction learned from a model.

## Schedules plus running balances

A forecast starts from a real, stored balance and walks forward, applying each
`expected` instance that active [schedules](../../how-to/schedules-and-bills/)
materialize on the horizon. The result is a daily series where every step is a
**named, dated event** — the rent on the 1st, the paycheck on the 15th — carrying
the running `balance_after` at that point. The narration and the chart come from
exactly the same data, because the events array *is* the chart.

## No statistical models, by design

There is no regression, no seasonality model, no machine-learned trend. That is a
choice, not a gap. A deterministic forecast is **explainable**: every projected
movement traces to a schedule you can inspect and edit, so when the number
surprises you, you can see precisely which expectation caused it. A statistical
forecast that is confidently wrong about your rent is worse than useless for
planning cash.

## Shortfalls

`forecast_balance(account, days)` (horizon defaults to 30 days) returns the daily
series, the projected minimum (`low`), and the account floor (`min_balance`, null
when unset; the headline then measures against zero). Calling it only reads: it
never emits an event.

The warning comes from a nightly scan instead. At 07:15 UTC Purser forecasts the
next 30 days of every open account that has a floor, and when a projection
crosses that floor it emits a `forecast.shortfall` event, once per account and
first shortfall date. The warning reaches you through the event stream, a
webhook, or the `whats_new` digest before the account actually runs dry, not
after. An account without a floor is never scanned, so set one on every account
you want watched: the floor is the `min_balance` field given when the account is
created (`POST /accounts`).

## Horizon semantics

The horizon is a count of days forward from today, up to 365. Schedules
materialize their instances on a rolling 60-day window; for slots past that the
forecaster expands the rules on the fly, so a 180-day call still shows every
occurrence. Those events are flagged `projected` — the rule expects them, but
no instance row exists yet to edit or match — while events with a
`transaction_id` are instances you can skip, override or mark paid. An
instance dated before today that has not been matched is flagged `overdue`:
it lands on day 0, because it is still due. The uncleared rows of a
[file import](../../how-to/import-qif-csv/) are `expected` too, so an outstanding
check lands on day 0 the same way until the matcher pairs it with the bank's
posted row. A slot you skipped never appears,
projected or not. The series stays daily, and the full daily series is always
available behind a `purser://` resource.

An instance the matcher has already **proposed** a bank row for is treated as
paid, pending your confirmation. That row — pending or posted — is already in
the starting balance, so landing the instance as well would subtract the money
twice. The forecast leaves it out, whether it is overdue or due within the
five-day match window, and the line is exactly what confirming the match would
give: the bank's amount, never a leftover difference. Reject the proposal and
the instance is back on the next forecast; confirm it (in the register, or
with `match` on the same pair) and it is settled for good. An account valued
by a stated figure rather than its postings (a loan or property with a
valuation) keeps such instances, because no bank row moves that figure.

## Where each amount comes from

Every event carries a `basis`: what produced its amount. A schedule's own
amount is `fixed`, or the estimate it follows — `last`, `rolling_avg_3`,
`rolling_avg_12`, or `counterparty_balance`, which for a card payment is the
card's current balance. An amount someone supplied is `override` (you pinned
the instance with a different amount or date) or `statement` (a recorded
[card statement balance](../../how-to/schedules-and-bills/#card-statement-balances)).
The REST forecast and the full daily series carry `basis` on every event;
`forecast_balance` names it only for `statement` and `override`, the amounts
an agent should be able to explain, and leaves it off the rest to stay
inside its budget.

## Estimate range

An estimated amount is a point taken from history, and history varies. So an
event whose amount a schedule estimates — a rolling average, the last
amount, or a card payment's current balance — also carries a `range`: the
lowest and highest of the schedule's six newest settled amounts together
with the event's own amount, so the point always sits inside. A card
payment's range is its past payments together with the card's current
balance (not the balance's swing from zero to its peak over a cycle). It
needs at least two settled amounts that differ; a fixed amount, an amount
you overrode, and a recorded card statement have no range, because nothing
about them is uncertain.

When any event has a range, the forecast also walks the horizon twice more:
once with every ranged event at its worst (outflows at their high, inflows
at their low) and once at its best. Each day then carries `low` and `high`
beside its balance, and the forecast reports the lowest point of each pass.
`forecast_balance` puts that under `low.range` — `low` and `low_date` for
the pessimistic pass, `high` for the optimistic one — and when only the
pessimistic pass dips below the floor, the headline says which event would
cause it: "It could dip to -125.00 on 2026-10-26 if Citi payment comes in
high." The `forecast.shortfall` event stays on the expected amounts, so a
wide range never raises an alert on its own. The forecast widget and the
Schedules page chart shade the band behind the line, and an event's
tooltip names its basis and range.

## What the event list shows

`forecast_balance` keeps its answer inside a token budget, so its `events`
list holds the movements that fit, not necessarily all of them. The movement
that produces the projected low is always listed, then the largest by
amount; pass `materiality` (a decimal string such as `"100.00"`) to list
only movements at least that large. Whatever is not listed is accounted for
in `omitted` — how many movements and their net — and `end_balance` is the
balance on the last day, so `start_balance` plus the listed amounts plus
`omitted.net` is always `end_balance`. `truncated` is true only when the
budget, rather than your `materiality`, left a movement out, and the
headline then says how many. Every day is in the series the payload's
`daily_uri` names: read it with `resources/read`, or with the `read_series`
tool when your client or gateway forwards tools but not resources. The
horizon (`days`) is 1 to 365, and every horizon the tool accepts is one the
series serves.

## Coverage: what the line is made of

Every forecast carries `coverage`: how many active schedules touch the
account, how many instances and projected occurrences went into the line, how
many open [suggestions](../../how-to/suggestions/) name the account, and
`matches_pending` — how many instances were left out as paid pending a match
confirmation, with their net in `matches_pending_net` (a decimal string,
present only when it is not zero). The headline names them too, so a caller
knows confirmations are outstanding: `list_unmatched` shows each one with
`proposed_posted_id`, the bank row it was paired with, and `match` on that
pair confirms it. A flat
line with `schedules_active: 0` is not a calm month — it is an account with
nothing modeled, and the headline says so instead of reporting that the
balance "stays above its floor".

Looking backward is the mirror image. One account's history is
[`balance_history(account, period)`](../../explanation/balances/), which reads the
same kind of recorded running balance in the other direction — and has the same
"the series IS the chart" property, except the numbers were stored rather than
projected. Net worth over time is the whole-portfolio counterpart — see design
section 8.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 6 (schedules and forecasting). The tool contract is in the
[MCP tool catalog](../../reference/mcp-tools/) under `forecast_balance`.
