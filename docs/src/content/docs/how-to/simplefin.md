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
the first-run wizard, or later into **Config -> SimpleFIN**. Setup tokens are
one-time secrets — paste them only into Purser, never into a chat. Purser
exchanges the token once for an encrypted access URL (stored encrypted at rest;
it never appears in logs) and discovers the accounts the connection exposes.

## Map feed accounts to ledger accounts

Discovered feed accounts are not ledger accounts until you map them. In
**Config -> SimpleFIN**, map each one to a Purser account (or create the account
inline). Only mapped accounts sync. Closing a ledger account unmaps it, and the
feed account reappears as unmapped — reopen and remap to resume.

## Sync schedule and the fetch budget

The schedule is a runtime setting in **Config -> SimpleFIN** (not an environment
variable): **daily** at 06:30 UTC (default), **every 12h**, **every 6h**, or
**hourly**. Every preset lands a run just before the matching job. SimpleFIN
Bridge allows **24 fetches per day**; the hourly preset sits exactly at that
budget, and the per-connection guard fails soft beyond it. Agents should call
`trigger_sync` when the user wants fresh data, not on every question.

## Pending transactions

Banks report a charge as **pending** before it posts, and Purser imports it as a
`pending` row. Every sync re-checks each open pending on a checking, savings,
cash or credit card account against the bank, not only the last week's: the
fetch reaches back to the oldest such pending (and a week before it), up to the
90 days one fetch can cover. Pendings on brokerage, loan and property accounts
never stretch the fetch: some investment feeds list every purchase as pending
indefinitely, and those accounts are valued by holdings or valuations anyway.
They are still settled whenever the fetch covers their dates.

- When the bank posts it, the posted row replaces the pending, whether it
  arrives with a new description or date or with the very same ones.
- When a pending disappears, Purser first looks for its posted row: same
  account, same amount, dated within 7 days either way.
- A pending the bank dropped with no posted row is voided and emits a
  `pending.vanished` event. The `whats_new` digest counts these and lists the
  ones above its materiality threshold. A dropped refund is worth chasing.

A pending older than 90 days is beyond what the bridge serves and stays as it
is. Void it yourself if your bank no longer shows it.

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

An incremental sync only reaches about 90 days back. To page **older** history,
use **Fetch older history** in Config (or `POST /connections/{id}/backfill`,
write scope). Backfill walks backward in windows of at most 90 days, archiving each
verbatim, and latches complete after two consecutive empty windows. It never
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

## Loan and property balances come from valuations

A loan or mortgage feed often reports a balance but few or no transactions.
Purser records each reported balance for a `loan` or `property` account as a
row in that account's **valuations** series, and every balance surface — the
accounts list, the account page headline, `GET /accounts/{id}/balance`, and
the agent tools — reports the latest valuation as the account's balance
(`balance_method: "valuation"`, with `balance_as_of` naming the valuation
date). When the feed does deliver transactions they stay visible in the
register as ordinary postings history, and the postings sum rides along as
`postings_balance`. An account with no valuation yet falls back to the
postings sum. Net worth uses the same rule, so the account page and
`net_worth` always agree.

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

**Config -> SimpleFIN** shows a **Feed accounts** table fed by
`GET /connections/{id}/discovered`: per account, the feed-reported
`balance_date`, the ledger balance, and any `drift` between them, recomputed
live from the latest archived payload. It is the convergence meter while you
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

## Pin an account's drift basis

When an account's history is short or misleading, pin its basis by hand: the
**Basis** select in the Feed accounts table offers **Auto** (showing the basis
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
