---
title: Schedules and recurring bills
description: Create schedules, let Purser materialize and match expected instances, and surface late bills and price changes.
---

A schedule is a recurring expectation — the $60.00 gym membership on the 1st,
the paycheck every other Friday. Purser materializes each schedule into
`expected` transactions, matches them against what actually clears, and tells
you when a bill is late or its price changed.

## Create a schedule

Recurrence is an RFC 5545 `RRULE` (the same syntax calendars use). Create a
schedule on the admin UI's **Schedules** page (its cadence picker writes the
rule for you), over REST (`POST /schedules`) or MCP with `create_schedule` — an
agent that parses a statement email calls this tool directly (Purser never
parses email server-side). The schedule is created active. An agent acting on
its own initiative passes `propose=true` instead, and the schedule lands as a
[suggestion](../../how-to/suggestions/) that your decision turns into the schedule
(`decide_suggestion`). A proposal can carry a `note` (up to 200 characters, such as the statement
line the agent read) for whoever decides it, and is refused when the account
already has a schedule for that payee or counterparty.

```json
{ "name": "Acme Gym", "account": "assets.checking",
  "rrule": "FREQ=MONTHLY;BYMONTHDAY=1", "dtstart": "2026-09-01",
  "amount": "-60.00", "payee": "Acme Gym",
  "category": "expenses.personal.fitness" }
```

Amounts are decimal strings; negative is money out. `amount_type` defaults to
`fixed` for a bill that is the same every period; `estimated` schedules take
their amount from confirmed matches (`estimate_basis`: `last`, `rolling_avg_3`,
`rolling_avg_12`) with `amount` as the fallback before any history exists.
Once two settled amounts differ, the forecast shows an estimated occurrence
with its range — the lowest and highest of the last six together with the
estimate (see [Estimate range](../../explanation/forecasting/#estimate-range)).

`dtstart` is the rule's **anchor** — the date the recurrence counts from. Rules
that name their day (`BYMONTHDAY=6,20`, `BYDAY=FR`) barely need it; rules that
do not (`FREQ=WEEKLY;INTERVAL=2` — every other week) take their phase from it,
so pass the last real occurrence. `FREQ` must be `DAILY` or coarser.

## Recurring transfers

A card payment, a loan payment, a 529 or IRA contribution is a transfer
between two of your accounts, not a bill against a category. Give the schedule
a `counterparty_account` instead of a `category`:

```json
{ "name": "Citi card payment", "account": "assets.checking",
  "counterparty_account": "liabilities.citicard",
  "rrule": "FREQ=MONTHLY;BYMONTHDAY=5", "dtstart": "2026-09-05",
  "amount_type": "estimated", "estimate_basis": "counterparty_balance" }
```

Its instances are two-leg expected transfers — the schedule account's leg is
`amount` (negative = money leaves it), the counterparty's leg the opposite —
so they show up in both accounts' forecasts, and when the bank rows on both
sides arrive and link into one transfer, the [matcher](../../explanation/matching/)
settles the expectation against that merged row (no double count). The
schedule is itself what links those rows when their descriptors say nothing
about a transfer, and the two sides may post on different days — see
[Scheduled transfers](../../how-to/transfers/#scheduled-transfers). The
`counterparty_balance` basis sizes each instance from the counterparty's
reported balance: "pay the card in full". For a card that is the **current**
balance, charges since the statement closed included, so record the
statement when it arrives (next section). The `whats_new` week of upcoming
bills reads an instance from the schedule account's leg, the side that pays:
a card or loan payment due this week is listed with its (negative) amount and
counts toward the total, while a transfer *into* the schedule's account (a
sweep from savings, say) is money arriving, not a bill.

## Card statement balances

A card payment estimated from the card's current balance usually runs high:
you pay the **statement balance** from the last close, and the card has new
charges since. Purser cannot read statements (SimpleFIN carries no statement
figure), so you or your agent record it against the payment it is due on —
on the Schedules page (**Statement** on a card payment's instance), over
REST, or with MCP `record_statement_balance`. An agent reading a statement
email ("statement balance 8,347.00, payment due October 19") makes one call:

```json
{ "account": "liabilities.citicard", "statement_balance": "8347.00",
  "due_date": "2026-10-19", "statement_date": "2026-09-22" }
```

`statement_balance` is the figure as printed: the amount owed, positive.
Purser finds the card's live payment instance due nearest `due_date` (within
14 days), sets it to the statement balance on both legs (`-8347.00` from
checking), and pins it, so the current-balance estimate no longer moves it;
the next cycle's payment keeps following the balance. Over REST (`POST
/schedules/statement-balance`) you can name the instance by `transaction_id`
instead of `account` and `due_date`. The statement, its closing date and
who recorded it stay on the instance, the instance and the
[forecast](../../explanation/forecasting/) report `basis: statement`, and
recording again replaces the figure. Reset the instance to go back to the
current balance. Recording needs write scope and works on credit card
payments only; a loan payment, or any other bill, is an ordinary override
(below). Paying the exact statement amount also lets the schedule link the
payment's bank rows (it vouches only for its exact amount).

## Rules and instances

The schedule is the durable **rule**. What you actually see in a register and
in the forecast are its **instances**: `expected` transactions materialized on
a rolling 60-day horizon, nightly and whenever a schedule is activated or
edited. Each instance fills one **slot** of the rule (its `occurrence_date`),
and materialization never creates a second instance for a slot that already
has one — matched, skipped, or edited.

Instances are yours to edit; the rule never overrides what you touched:

- **Override** an occurrence — a different amount ("this month's card payment
  is 8,067.37") or date ("it lands on the 3rd") — with the ordinary transaction
  edit (`PATCH /transactions/{id}`, MCP `override_instance`). The instance
  becomes **pinned**: estimate refreshes and rule edits leave it alone. On a
  transfer schedule both legs move: the amount is the schedule account's leg
  in the schedule's sign (`-8067.37` for a card payment from checking), or the
  leg of the account a PATCH names in `account` (`{"amount": "8067.37",
  "account": "liabilities.citicard"}` from the card's side). An amount whose
  sign would reverse the transfer is refused. A card's statement balance has
  its own path, which keeps the statement on the instance (see
  [Card statement balances](#card-statement-balances)).
- **Relabel** an occurrence — a different description or payee. A label is not
  an override: the instance stays un-pinned and keeps following the estimate.
- **Reset** an overridden occurrence (`POST /transactions/{id}/reset-instance`,
  MCP `reset_instance`, write scope; the **Reset** button on a pinned row in
  the Schedules page). The pin is cleared and the instance takes the rule's
  date (its slot), name, payee and current estimate again, and a recorded
  card statement is removed. If the rule no longer has that slot, or
  estimates nothing, the instance is dropped instead.
- **Skip** an occurrence (`POST /transactions/{id}/void`, MCP `skip_instance`).
  The slot stays filled, so the occurrence does not come back.
- **Mark paid** by matching the instance to a posted row (`POST
  /matching/match`, MCP `match`). When the matcher already proposed that very
  pair, this confirms the proposal — the unmatched report (`GET
  /matching/unmatched`, MCP `list_unmatched`) names the proposed bank row as
  `proposed_posted_id` — and a row proposed for a different bill is refused,
  with the error naming that bill.

The rule side: an `estimated` schedule refreshes its live, un-pinned instances
whenever a new confirmed match moves the estimate (audited as
`schedule.refresh`). Editing the rule (`PATCH /schedules/{id}`, MCP
`update_schedule`) resyncs: slots the new rule no longer produces are voided
and released, survivors are re-estimated, missing slots materialize — and
editing back re-fills the released slots. **Renaming** a schedule (or changing
its payee) relabels its live instances, pinned ones too: every instance whose
description is still the old name takes the new one, and so does a payee that
is still the old one — a schedule without a payee uses its name as its
instances' payee. A description or payee you set yourself is kept. **Pause** stops expecting (live
un-pinned instances are dropped and come back on **resume**); **end** is
terminal and keeps history (`POST /schedules/{id}/pause`, `/resume`, `/end`;
MCP `end_schedule` with `action` set to `pause`, `resume` or `end`). `GET /schedules/{id}` lists the live instances with
their slot, pinned and overdue flags; `next_due` is always the earliest live
instance — an overdue one counts, because it is still due. In the admin UI the
same actions live on the Schedules page (expand a schedule for its instances;
pinned rows carry Reset) and on the account register, where a materialized row carries a `scheduled`
badge with Override, Mark paid and Skip.

## Expected instances and matching

The [matcher](../../explanation/matching/) reconciles each instance against the real
posted or pending row when it arrives. **The posted amount always wins** — the
expectation is replaced by reality, and the difference is recorded as a
variance. Variance feeds future estimates, and a breach beyond the threshold
emits a `variance.threshold` event.

A match to a **pending** row follows that row. When the bank posts it, the
match moves to the posted row and the bill stays paid (a proposed match is
decided again against the posted row). If the bank drops the
pending instead, the match is released and the instance comes back as
expected, in the same slot and with any override you made, so it can go late
or match the charge the bank really posts. Voiding the pending yourself
(`POST /transactions/{id}/void`) does the same: a confirmed match is released
the same way, and a proposed match is withdrawn so the bill can be matched
again.

While a match is only **proposed**, the bank row is already in the balance, so
the [forecast](../../explanation/forecasting/) and the `whats_new` week of upcoming
bills treat the instance as paid, pending your confirmation, and the forecast's
coverage counts it in `matches_pending`. Estimates only learn from posted
amounts: a pending amount can still change, so it moves the estimate (and
raises a `recurring.price_changed` event) only once the posted row lands —
whether the bank posts it, you post the pending yourself
(`POST /transactions/{id}/post`), or the bank's posted row for the other side
of a pending transfer arrives and posts the whole transfer.

## Late bills

When an expected instance passes its window unmatched, Purser emits a
`bill.late` event. Late bills surface three ways: the event stream (and any
webhook), the `whats_new` MCP digest, and the Dashboard's **Needs attention**
card, which collects late bills alongside the suggestions awaiting your
decision. A late bill the matcher has proposed a bank row for shows as that
proposal's match card instead: accept it to mark the bill paid, or reject it
so that row is never proposed for the bill again.

## Detected schedules

Purser also proposes schedules it finds for you. Every night (and on `POST
/schedules/detect` / MCP `detect_schedules`) it scans your history per account
and counterparty — a payee for ordinary bills, the other account for transfers
such as card payments, loan payments and contributions — and each recurring
pattern becomes a **suggestion** in the [suggestions inbox](../../how-to/suggestions/)
with the evidence attached: the occurrences, the cadence it found (weekly,
every two weeks, semi-monthly, monthly, every two/three/six months, yearly), how
regular the gaps were, and how steady the amount was. A varying amount is not a
veto — card payments that swing threefold are still monthly; they come through
as `estimated` (paying the card's balance when the counterparty is a card). A
single skipped month is tolerated; a pattern that stopped is not proposed.

Adding a **credit card or loan** account triggers the same scan for payments to
it right away (`source = account_added`). Detected patterns **never
auto-activate** — accepting the suggestion is the only path to
an active schedule, and rejecting one is remembered.

## Price changes

`list_recurring` is the subscription radar: every active outflow schedule with
an honest monthly-equivalent total (annual and semi-annual bills normalized),
its next due date, and its last price change, all computed server-side — never
sum the rows yourself. A schedule you accepted from a suggestion is as active
as one you created: its `source` (`manual`, `detected` or `agent`) only records
where it came from. Patterns still waiting for your decision are not listed;
`suggestions_open` counts them, and they live in the
[suggestions inbox](../../how-to/suggestions/). In MCP Apps hosts the recurring
widget shows that count as a footer. When a recurring charge posts at a new amount, Purser emits a
`recurring.price_changed` event and the change lands in that price history, so
"what am I paying, and what changed?" is one tool call.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
sections 6 (schedules and forecasting) and 5 (matching). See the
[MCP tool catalog](../../reference/mcp-tools/) for `list_recurring`, `create_schedule`,
`update_schedule`, `end_schedule`, `skip_instance`, `override_instance`,
`reset_instance` and `record_statement_balance`.

Recurrence detection counts each correction chain once, using its latest
replacement. Editing a posted bank transaction does not double its amount in
the history, whether the correction was made by a person, an agent or a rule.
