---
title: Connect banks with SimpleFIN
description: Claim a SimpleFIN setup token, map feed accounts to the ledger, schedule syncs, backfill history, and scope each account with feed_start.
---

SimpleFIN Bridge is Purser's live feed path: an aggregator hands the Bridge your
bank data, and Purser fetches it on a schedule. Every payload is archived
verbatim to the append-only `raw_fetches` archive before anything touches the
ledger, and every imported row traces back to those bytes.

A row's identity is its content — account, date, amount and description —
never the bank's transaction id, which can change between fetches. Two
identical charges on one day (the same merchant, the same amount) are still two
rows: within one fetch, the second identical row is the second **occurrence**
of that content, an identity of its own. Every sync re-fetches the last seven
days so pending charges can settle, and that overlap adds nothing, because each
fetch numbers its identical rows from one again and lands on identities already
in the ledger.

## Connect

Get a **setup token** from your SimpleFIN Bridge account page and paste it into
the first-run wizard, or later into **Settings -> Connections**. Setup tokens are
one-time secrets — paste them only into Purser, never into a chat. Purser
exchanges the token once for an encrypted access URL (stored encrypted at rest;
it never appears in logs) and discovers the accounts the connection exposes.

## Map feed accounts to ledger accounts

Discovered feed accounts are not ledger accounts until you map them. The setup
wizard's map step covers the accounts found at setup; an account your bank adds
later shows as **not in Purser yet**, first under its bank, in **Settings -> Connections**.
Select **Map** beside it (admins only) and either create a new Purser account
(name, type and ledger path; cards and loans default under `liabilities.`) or
pick an existing open account in the same currency. Picking an account that
another feed account already feeds moves the feed onto the new one. The
already-fetched transactions import as soon as you map. **Start from the
bank's balance** (ticked for a new account) then adds one starting entry so
the account matches the balance the bank reports:
[Start an account from the bank's balance](../starting-balance/). Only mapped accounts
sync. Closing a ledger account unmaps it, and the feed account reappears as
unmapped — reopen and remap to resume.

### What Purser proposes

SimpleFIN states an account's name, its bank, its currency, its balances and
(for investment accounts) its holdings. It does not state what kind of account
it is, and **the account type cannot be changed after mapping**. Purser
therefore proposes one and says what the proposal rests on:

| The feed shows | Proposed type |
| --- | --- |
| A word for the product in the name ("Savings", "Visa", "Mortgage") | That product |
| A cash product's name, and holdings as well | That product, marked unsure: say whether it is really an investment account |
| No such word, and holdings | Brokerage |
| No such word, and a debt with credit left | Credit card |
| No such word, and a debt with no transactions | Loan (marked unsure) |
| Nothing that says | Checking (marked unsure) |

It also proposes a name (the bank's, without the masked account digits) and a
ledger path, both kept clear of accounts you already have, and it points out
an account that a second login at the same bank delivers again (a joint
account), which should be ignored rather than mapped twice. The map form
starts from this proposal; change anything before you confirm.

### Let your agent map them

An agent with a `read + write` token can do the mapping. `map_feed_accounts`
with no arguments answers the plan above for every account still waiting and
changes nothing. The agent shows you that plan, and after your answer applies
the lines you agreed to with your corrections, in one batch that lands whole
or not at all. It names each account by an opaque reference: the bank's
account ids, the masked digits and the balances are never shown to it. Over
REST the same call is `POST /connections/map-accounts`.

```json
{
  "plan": [
    {
      "ref": "fa_k3v9q2m7x4tb",
      "institution": "Example Bank",
      "bank_name": "Everyday Checking",
      "action": "map",
      "name": "Everyday Checking",
      "type": "checking",
      "type_basis": "name",
      "confident": true,
      "path": "assets.everyday_checking",
      "starting_balance": true
    }
  ]
}
```

An agent can also map a bank account onto an existing account (`account` on
the line), but only one that takes no feed yet. If the account already takes
another bank account's feed, the line is refused and the answer names that
feed: mapping onto it would replace the feed, the other bank account would wait
to be mapped again, and the new bank's rows would land in the account. Only an
admin can replace a feed: use the map form, which marks such accounts "replaces
its current feed", or send `"replace_feed": true` with an admin token. The
answer then names the feed it replaced, in the dry run too.

Mapping cannot be undone from the app. To remove an account that was mapped
with the wrong type, use the `delete-account` command described in the
[CLI reference](../../reference/cli/), then map the bank account again.

Until you decide, the Dashboard lists each such account under **bank accounts
not in Purser yet**, with a link that opens its row.

### Ignore an account you do not want

Select **Ignore** instead of **Map** when the account should stay out of the
ledger. The usual case is a joint account that two bank logins can both see:
SimpleFIN delivers it once per login, and only one copy should be mapped. The
page says so when an unmapped account has the name of a mapped one. An ignored
account is listed last with **Track again**, is no longer reported as waiting
(in the app, or to agents as `not_connected`), and nothing about it is
imported or deleted. Mapping it later imports its fetched transactions as
usual.

For checking, savings, cash and card accounts, discovery also shows the
bank's `available_balance` when it differs from the balance: the cash you can
spend, or the credit left on a card.

Each discovered account names its bank login: `org` is the login's name and
`conn_id` its SimpleFIN id. Two logins at one bank (for example, two people who
share a joint account) show as two logins, each listing its own copy of the
shared account; map one of them. If two accounts in one fetch arrive with the
same id, both are flagged `ambiguous` and neither syncs until the bank sends
distinct ids.

## Read bank balances without administration

`GET /accounts/{id}`, `GET /accounts/{id}/balance` and MCP `account_balance`
include the same `bank_snapshot` with ordinary **read** scope. No connection
discovery or administrator identity is needed. The account detail and balance
surfaces include it; account lists stay compact. Reading it does not sync the
feed, record a reconciliation, learn a new basis or change financial data.
Normal read auditing still applies.

- `kind: latest` means the most recent archived sync report, even if the
  enclosing ledger balance was requested with a historical `as_of` date.
- `balance` is the bank-reported balance; `available_balance` is separate and
  retained even when equal to it. Available balance can mean remaining credit
  on a card, and is neither ledger cash nor simply balance minus pending items.
  `available_balance_status` distinguishes reported, missing and invalid values.
- Amounts are decimal strings in `currency`. Missing or invalid amounts are
  null, never zero. `source` identifies SimpleFIN without revealing bank numbers,
  external identifiers, credential URLs or other accounts in the payload.
- `balance_at` preserves a real source instant (epochs rendered in UTC; an ISO
  timestamp retains its offset). This does not identify the bank's local time
  zone. `balance_date` is the UTC date of an instant, or the original date-only
  value. `time_precision` is `timestamp`, `date` or `unknown`; date-only values
  never acquire a midnight timestamp. `fetched_at` is the separate ingestion
  timestamp, not the time the bank measured its balance.
- `freshness` is `fresh`, `stale`, `future` or `unknown`. `age_days` compares
  `balance_date` with the ledger day; staleness uses `feed_stale_days`.
- `status` distinguishes disconnected/closed/disabled accounts, no snapshot,
  an account missing from the payload, ambiguous provider IDs and missing or
  invalid bank balances. Unknown values and comparisons stay null.

For eligible cash/card accounts (and opted-in manual brokerage cash),
`comparison` includes live posted-only and pending-inclusive totals, the
selected `ledger_balance`, effective `basis`, `basis_learned`, `basis_pinned`
and exact `difference = bank balance - selected ledger balance`.
`ledger_cutoff: all_recorded_dates` explicitly includes future-dated live
postings. It is not a historical reconstruction at the bank timestamp.
Unavailable comparisons have a `comparison_unavailable` reason, including
unknown/mismatched currency or an ineligible account type.

`matches_either_basis` is separate from the exact difference: the existing
drift-warning policy considers the account clean if either ledger basis
matches. For example, a bank balance of `"100.00"`, posted balance of
`"100.0000"` and pending-inclusive balance of `"85.0000"` give a difference
of `"15.0000"` when pending-inclusive is pinned, but `matches_either_basis`
is true. Reading the snapshot does not change that alert policy or its gates.

## Bank errors and connection status

Purser asks SimpleFIN for protocol version 2, which tags every error with a
code and, where it applies, the bank login or account it concerns:

| Code | Meaning | What to do |
| --- | --- | --- |
| `con.auth` | A bank login needs attention | Log in to that bank again in SimpleFIN Bridge |
| `act.failed`, `act.missingdata` | One account's data could not be fetched completely | Usually clears on a later sync |
| `gen.auth` | SimpleFIN rejected Purser's access | Reconnect with a new setup token |
| `gen.api` | A note for developers, such as a request window longer than the Bridge recommends | Nothing |

A sync whose fetch carries a `con.*` or `act.*` error, or `gen.auth`, sets the
connection's status to `warning`; the next clean fetch sets it back to
`active`. `GET /connections` lists the latest fetch's errors as `last_issues`
(code, message, `conn_id`, `account_id`) beside the plain messages in
`last_errors`, and a sync result carries the same `issues`. A server that still
answers in version 1 sends messages without codes; they read as code `gen.`,
and any of them sets `warning`.

## Know when a bank stops updating

A bank can stop updating while every sync still succeeds: SimpleFIN keeps
returning the last balance it saw. After each sync Purser checks every bank
login and every mapped account and raises an event when a problem starts and
again when it clears:

- `feed.auth_required` — a bank login needs you to log in again;
- `feed.account_failed` — an account failed to download on two syncs in a row;
- `feed.stale` — an account's bank balance date is more than
  `feed_stale_days` old (default 4; set it with `PUT /config` or
  `PURSER_FEED_STALE_DAYS`);
- `feed.recovered` — one of these cleared.

The agent digest (`whats_new`) lists what is open in its `feed` section on
every call until it is fixed, so a daily brief keeps naming the bank. When a
bank needs a new login, log in again in SimpleFIN Bridge; the next sync clears
the problem.

## Recover what a bank outage missed

While a bank is stuck, every sync still succeeds, so each sync asks only for
the last few days. When the bank comes back it has the outage's transactions,
older than that window. Purser notices the account's balance date jump past
the window and, in the same sync, fetches that account again from the date it
froze (focused requests of at most 45 days each, reaching at most 90 days
back, the Bridge's limit). The
sync result and audit row report it as `gap_repair`. If the day's fetch budget
is spent or the request fails, the repair is kept and the next sync runs it.
When the outage began more than 90 days ago, `gap_repair.unreachable_before`
marks the date before which rows must be imported by hand
([QIF or CSV](../import-qif-csv/)).

For a gap the automatic repair did not see (for example one from before this
feature), re-fetch a range yourself:

```bash
curl -X POST "$PURSER_URL/connections/$CONNECTION_ID/refetch" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d '{"start": "2026-08-10", "end": "2026-10-12", "accounts": ["assets.checking"]}'
```

`end` defaults to today and may be at most 90 days after `start` (fetched in
45-day pages, each one of the day's 24 fetches); `accounts`
are ledger paths and default to every mapped account. Agents use the MCP tool
`refetch_feed`. Rows already in the ledger are skipped, so a repeat run imports
nothing.

## Bank health in the app

**Settings -> Connections** shows the same picture for people. Each connection
opens with a summary in words (how many banks need a new login, how many
accounts are not in Purser yet or have stopped updating), then one list of
every bank login with its status (ok, needs login, error) and each
account it delivers with the Purser account it feeds (or "not in Purser yet"), its
bank balance date and age (stale ones flagged), and what the latest sync
carried. **Re-fetch a date range** runs the refetch described above for a range
and the accounts you pick. **Stale after (days)** next to the sync schedule sets
`feed_stale_days`. After **Sync now**, a note says what an outage repair
recovered or why it was postponed.

## See every feed account (agents)

`GET /connections/inventory` (read scope) lists every account SimpleFIN
delivers, grouped by bank login, whether or not a ledger account takes it: the
login's error, and per account the ledger account it feeds (or none), how many
days old its bank balance is (`stale` past `feed_stale_days`), how many
holdings and transactions the latest sync carried, and any account error. It
answers "did my new account show up?" without admin rights. API tokens without
admin scope never see SimpleFIN ids, and the masked account numbers banks put
in names (`(1234)`, `*1234`) are removed. Agents use the MCP tool
`feed_status`: one line per bank login plus only the accounts that need
attention (`not_connected`, `stale Nd`, an error code). An ignored account is
not reported as `not_connected`.

## Sync schedule and the fetch budget

The schedule is a runtime setting in **Settings -> Connections** (not an environment
variable): **daily** at a time you choose (06:30 by default), **every 12h**,
**every 6h**, or **hourly**. The interval presets land a run just before the
matching job. SimpleFIN
Bridge allows **24 fetches per day**; the hourly preset sits exactly at that
budget, and the per-connection guard fails soft beyond it. Each run starts
at a random point in the 10 minutes after its slot, as SimpleFIN asks apps to
spread their fetches. Agents should call
`trigger_sync` when the user wants fresh data, not on every question.

A sync whose time passes while the machine is asleep or Purser is stopped is
not lost: Purser runs it once when it wakes or starts, however many were
missed. A connection whose last attempt failed is not tried again at every
restart.

The daily time is a clock time in the household time zone (`PURSER_TIMEZONE`,
**Settings -> General**), stored as the hour and minute of `sync_schedule` in
`PUT /config`, and the scheduler reads it in that zone. It stays at the same
time on the clock when daylight saving starts or ends: 06:30 in New York is
06:30 in winter and in summer. On the night the clocks change, a time the
clocks skip runs an hour later and a time in the repeated hour runs twice.
Changing the household time zone keeps the clock time and moves the sync to
the new zone. The interval presets run on fixed UTC times.

Members (read + write) see each connection's summary, its banks and accounts,
**Sync now** and, under **Maintenance and schedule**, **Fetch older history**
and **Re-fetch a date range**. Bank balances, the comparison with Purser,
**Map** and **Ignore**, adding a connection and the schedule are admin-only.
The schedule, older history, re-fetching, the comparison basis and adding a
connection all sit under **Maintenance and schedule** at the foot of the page.

Every request asks for at most 45 days, the span SimpleFIN recommends: a first
sync (88 days) takes two requests, and a long backfill, an outage repair or a
refetch takes one request per 45 days. Each counts against the daily budget.

## Pending transactions

Banks report a charge as **pending** before it posts, and Purser imports it as a
`pending` row. Every sync re-checks each open pending on a checking, savings,
cash or credit card account against the bank, not only the last week's: the
fetch reaches back to the oldest such pending (and a week before it), up to
44 days (one request within the Bridge's recommended 45 days). Pendings on brokerage, loan and property (asset) accounts
never stretch the fetch: some investment feeds list every purchase as pending
indefinitely, and those accounts are valued by holdings, valuations or the
reported balance anyway.
They are still settled whenever the fetch covers their dates.

- When the bank posts it, the posted row replaces the pending, whether it
  arrives with a new description or date or with the very same ones.
- When a pending disappears, Purser first looks for its posted row: same
  account, same amount, dated within 7 days either way.
- A pending the bank dropped with no posted row is voided and emits a
  `pending.vanished` event. The `whats_new` digest counts these and lists the
  ones above its materiality threshold. A dropped refund is worth chasing.

A pending older than 44 days no longer widens the fetch and stays as it is
unless a backfill or refetch covers its date. Void it yourself if your bank no longer shows it.

### Re-described rows

A fetch that reaches back past the last week also brings older posted rows the
ledger already holds. Banks sometimes rewrite a posted row's description weeks
later (a merchant name gains a suffix, say), and because the description is part
of a row's identity, that copy would otherwise land as a second row. Purser
skips it as **re-described** when the account already has a posted row from an
earlier sync on the same date for the same amount, under a description the
bank no longer lists. Sync and backfill results count these as
`skipped_redescribed`. A
row that posted late with no such neighbour imports normally, and so does a
row a disappearing pending may have posted as.

## Backfill older history

The first sync reaches 88 days back. To page **older** history,
use **Fetch older history** under **Maintenance and schedule** in **Settings -> Connections** (or `POST /connections/{id}/backfill`,
write scope). Backfill walks backward in windows of at most 45 days (the
Bridge's recommended span), archiving each verbatim, and latches complete after
four consecutive empty windows (180 days with nothing). It never
moves the forward sync anchor, shares the daily fetch budget (reserving two
fetches for scheduled and manual syncs), and resumes from a saved cursor if
interrupted. A result may carry a `qif_overlap` warning when a backfilled range
meets manually imported rows; identical rows still dedup by content hash. You
can scope a backfill to specific accounts so onboarding one need not re-fetch
the rest.

## Scope an account with feed_start

`accounts.feed_start` is a per-account date: the importer skips feed rows dated
**strictly before** it, across sync, backfill, and map re-import alike. Set it
to partition an account's history cleanly — manual import owns everything before
the boundary, the feed owns everything on or after it. It is import-time only:
setting it never deletes rows already in the ledger, and skips are counted as
`skipped_before_feed_start`. Set it via `PATCH /accounts/{id}` (an explicit null
clears it). The typical rollout is to set it at the manual/feed boundary when
you first map the account.

## Fix a sign-inverted feed with feed_invert

Some institutions deliver an account's transaction stream with the signs
flipped — deposits arriving as negative amounts (observed with Fidelity
brokerage, HSA, and 401(k) accounts). The symptoms are an asset account
whose balance trends negative and transfers that never pair (both legs
carry the same sign). Set `accounts.feed_invert` via `PATCH /accounts/{id}`
(admin, like `feed_start`) and the importer negates every feed amount for
that account from then on. The inversion happens after content hashing, so
a row's identity stays exactly what the bank sent: flipping the flag never
re-imports history, and rows already in the ledger are never rewritten —
repairing existing wrong-signed rows is a separate, deliberate admin
operation.

## Loan balances come from valuations

A loan or mortgage feed often reports a balance but few or no transactions.
Purser records each reported balance for a `loan` account as a
row in that account's **valuations** series, and every balance surface — the
accounts list, the account page headline, `GET /accounts/{id}/balance`, and
the agent tools — reports the latest valuation as the account's balance
(`balance_method: "valuation"`, with `balance_as_of` naming the valuation
date). When the feed does deliver transactions they stay visible in the
register as ordinary postings history, and the postings sum rides along as
`postings_balance`. An account with no valuation yet falls back to the
postings sum. Net worth uses the same rule, so the account page and
`net_worth` always agree.

An account mapped as `property` is a ledger instead: when its reported
balance differs from its balance on the sync's day, Purser posts a value
change for the difference (see
[Track a house or other asset](../hand-valued-assets/#an-asset-with-a-bank-feed)).

## Brokerage balances add holdings to cash

A brokerage feed reports cash transactions and a **holdings** snapshot side by
side. Purser stores each as what it is — cash movements become ordinary
postings, holdings become rows in `holdings_snapshots` — and every balance
surface reports the two **added together**: the postings sum plus the market
value of the latest holdings snapshot at or before the date you asked about
(`balance_method: "postings+holdings"`, with `balance_as_of` naming the
snapshot date). The cash-only figure rides along as `postings_balance`.

The difference from a loan is that a valuation *replaces* the postings sum
while a market value *adds* to it — a brokerage account's cash is real money
the feed already told you about, so dropping it would understate the account.

An account with no snapshot yet reports the postings sum alone, and a closed
account keeps its postings answer so a stale snapshot cannot resurrect it.
Net worth uses the same rule, so the account page and `net_worth` agree here
too. See [Balances](../../explanation/balances/) for all three methods and the two
bases they are applied to.

### When the feed sends no holdings

Some brokerage feeds deliver the account's cash but no holdings at all. Switch
such an account's **holdings source** to `manual` (admin,
`PATCH /accounts/{id}` with `{"holdings_source": "manual"}`) and record its
positions by hand: the feed keeps importing the cash, stops recording holdings
for that account (`holdings_skipped_manual` in the sync result), and the
positions you record are priced daily and added to the balance the same way.
See [Track holdings by hand](../../how-to/manual-holdings/).

## Watch accounts converge

**Settings -> Connections** shows an admin, per account, the bank's balance and
its date and a **Compared with Purser** column (matches, or how far off), from
`GET /connections/{id}/discovered`, recomputed live from the latest archived
payload. **How balances are compared**, under **Maintenance and schedule**,
lists both balances and the `drift` side by side. It is the convergence meter while you
onboard or backfill — expect drift to shrink toward zero as history fills in.
After every successful sync, Purser also runs a balance-drift check that emits a
`balance.drift` event when a mapped cash or card account's reported balance
disagrees with the ledger. It is a **warning only**: it never fails a sync,
never adjusts the ledger, and never writes financial rows. See
[Events and webhooks](../../reference/events-webhooks/) for the event, and
[`whats_new`](../../reference/mcp-tools/) for the agent-facing digest. When the
drift is real and you have the statement balance,
[reconcile the account](../../how-to/reconcile-an-account/).

Banks differ on whether the balance they report counts pending charges, so
the drift is measured on the basis each account's bank uses, reported as
`drift_basis` and shown next to the drift: `posted_only` when the bank leaves
pendings out, `pending_inclusive` otherwise. Purser learns the basis per
account from its recent syncs (the one whose drift held steady wins), so a
standing gap at a bank that leaves pendings out stays one number while charges
come and go instead of moving with every pending. The same basis rides on the
`balance.drift` event (`mode`) and on each `whats_new` drift line (`basis`).
An account's first syncs have nothing to learn from yet, so the smaller of the
two drifts decides until its history tells them apart. The ledger balance
column always includes pendings.

A brokerage account's feed balance is usually its total value, so brokerage
accounts are left out of the check. The exception is a cash sweep whose
positions you track by hand: an admin can say its feed balance is its cash
(`PATCH /accounts/{id}` with `{"feed_balance_is_cash": true}`, or **Feed
balance is cash** on the account page), and the account is then checked like a
checking account. See
[Track holdings by hand](../../how-to/manual-holdings/#check-a-cash-sweeps-balance).

## Pin an account's drift basis

When an account's history is short or misleading, pin its basis by hand: the
**Basis** select under **Maintenance and schedule -> How balances are compared** offers **Auto** (showing the basis
Purser learned) and the two pins. Over the API, an admin sends
`PATCH /accounts/{id}` with `{"drift_basis_override": "posted_only"}` or
`"pending_inclusive"`; `null` clears the pin and absent leaves it alone. Only
checking, savings, cash and credit-card accounts take a pin (the drift check
runs on nothing else), and the change is audited.

A pinned basis wins over the learned one everywhere it is reported:
`GET /connections/{id}/discovered` sets `drift_basis_pinned` (and still shows
the learned pick as `drift_basis_learned`), the `balance.drift` event carries
`basis_pinned: true`, and the `whats_new` drift line carries `pinned: true`.
The pin only chooses which drift is reported: an account whose bank balance
matches either basis is still in sync. Purser keeps learning underneath the
pin, so clearing it brings back exactly the basis its history picks. Because
setting or clearing a pin changes the reported drift, it can open one new
`balance.drift` episode.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
sections 7.1 (connect, backfill, feed_start) and 7.3 (balance-drift check).
