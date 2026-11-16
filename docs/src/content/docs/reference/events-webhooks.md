---
title: Events and webhooks
description: The durable event stream, every event type, and HMAC-signed webhook delivery.
---

Every notable state change appends a row to a durable `events` stream in the
same transaction as the change itself. Consume it two ways: poll
`GET /events` (read scope, cursor-paginated — the same stream the `whats_new`
MCP tool digests), or register webhooks.

## Event types

| Type | Fires when |
| --- | --- |
| `account.closed` | An account is closed (balancing adjustment posted, feed unmapped). |
| `account.deleted` | A duplicate account is hard-deleted via the guarded CLI. |
| `account.feed_rows_purged` | An account's feed-origin rows are hard-deleted via the guarded `purge-feed-rows` CLI. |
| `account.moved` | An account moves to a new path (`PATCH /accounts/{id}` with `path`). The payload carries the new `path`, the `from_path`, and `accounts_moved` — the account plus every account beneath it. |
| `account.reconciled` | An account is reconciled to a statement balance (adjustment posted when needed, claim recorded). Payload below. |
| `account.reopened` | A closed account is reopened. |
| `account.retired_rows_purged` | Import rows with no source fetch are removed by creation batch via the guarded `purge-retired-rows` CLI; the payload names the `batches` and counts `transactions_voided` and `transactions_deleted`. |
| `balance.drift` | A synced account's feed-reported balance disagrees with the ledger (once per drift episode; cash and card accounts, plus a hand-tracked brokerage account whose feed balance an admin marked as its cash); the payload's `mode` names the basis the delta is measured on, `posted_only` or `pending_inclusive`, and `basis_pinned: true` appears only when that basis is the account's admin-set pin rather than learned. |
| `bill.late` | An expected transaction (a schedule instance, a manual bill, or an uncleared imported row) passes its date unmatched. |
| `feed.account_failed` | A mapped SimpleFIN account reports an account-level error (`act.failed`, `act.missingdata`) on two syncs in a row. Once per episode: the payload carries `account` (the path), `institution`, `code` and `message`. Usually clears on its own; `feed.recovered` follows the first clean sync. |
| `feed.auth_required` | A SimpleFIN bank login reports a connection-level error (`con.auth`: log in to that bank again in SimpleFIN Bridge). Once per episode, from the first sync that reports it: the payload carries `institution` (the login's name), `conn_id`, `code` and `message`. |
| `feed.recovered` | A feed problem clears. The payload's `kind` says which: `auth` (with `institution`, `conn_id`), `account_failed` (with `account`, `institution`) or `stale` (with `account`, `institution`, the new `balance_date` and `stale_since`, the frozen date). |
| `feed.stale` | A mapped SimpleFIN account's bank balance date is more than `feed_stale_days` (default 4) days old: the bank has stopped updating, often because its login needs attention. Once per account and frozen date, so a bank that updates and freezes again fires again: the payload carries `account`, `institution`, `balance_date` and `stale_days` (its age). |
| `forecast.shortfall` | The nightly forecast scan (07:15 UTC) projects an account that has a floor (`min_balance`) below that floor within the next 30 days. Accounts without a floor are not scanned, even when projected below zero, and a `forecast_balance` call never emits it. The floor is set at creation or later through `PATCH /accounts/{id}`, MCP `update_account` or the app (write scope); the next scan uses the change, and clearing it stops the event. Once per account and first shortfall date: the payload carries `account` (the path), `date` (the first day below the floor), `low` (the 30-day projected minimum) and `min_balance`. |
| `match.proposed` | The matcher proposes an expected-vs-imported pair. |
| `match.confirmed` | A match is confirmed. |
| `pending.vanished` | A pending imported transaction disappears from the feed with no posted successor (none of the same amount on its account within 7 days), and is voided. `whats_new` counts these. |
| `position.updated` | A hand-tracked position changes: opened, renamed, an event recorded or reversed, closed, or reopened, or one of its lots voided or its basis corrected. The payload names the `account` path, `position_id`, `symbol` and `change` (`opened`, `renamed`, `recorded`, `reversed`, `closed`, `reopened`, `lot_voided`, `lot_basis`); a rename adds the new `name` (null when cleared), a recorded or reversed event adds `kind`, `occurred_on` and `quantity_held` (today's share count), a recorded trade that moved cash adds its cash row's `cash_transaction_id`, and a lot change adds `lot_id`. |
| `holdings.captured` | A lot or award capture is applied ([Track tax lots and equity awards](../../how-to/lots-and-awards/)). The payload carries `account` (the path), `kind` (`lots` or `awards`), `as_of`, `completeness`, `changes` (counts by kind), `repeat` (the same holdings as the previous capture: freshness only) and, for lots, `symbols` and `skipped_symbols`. An exact retry of an applied capture emits nothing. Never a lot or grant reference. |
| `award.updated` | An equity award changes: a vest recorded (`change` `vested`, with `vest_date`, `net_quantity` and `linked`), a vest forfeited or set back to scheduled (`forfeited`, `scheduled`), or its units corrected (`updated`). The payload carries `award_id` and `symbol`, never the grant reference. |
| `price.fetch_failed` | The price fetch (daily, or an admin's fetch-now) could not get or store some of a symbol's closes: the provider refused or failed a request, the store failed, or the answer carried closes out of range, which are dropped. Once per symbol per day for a failed request, and once more that day when closes were dropped, however often the fetch runs; the payload carries `provider`, `symbol`, `status` (the HTTP status, `200` for dropped closes, or null for a transport or store failure) and `reason` (a fixed phrase such as `refused` or `2 close(s) out of range dropped`, or an exception's class name). Never the API key or a provider message. |
| `price.stale` | A symbol held by a hand-tracked position has a newest close older than the staleness threshold, or no close at all. Once per symbol per stale close: the payload's `price_as_of` (null when there is none) and `stale_days`; a fresher close ends it, and a later stale close fires again. |
| `holding.unpriced` | A brokerage feed reports a held fund with no value (0 or none: a plan-only fund no provider prices) and no recorded price values it. Once per account and symbol when it turns unpriced, re-armed once the position was priced: the payload carries `account` (the path), `symbol`, `quantity`, `as_of` (the date of the set it sits in) and `unpriceable`. `record_price` under the fund's ticker values it; when `unpriceable` is true the feed sent a description instead of a ticker, and no recorded price can value it. |
| `holding.period_open` | A brokerage account switched back to the feed still counts its hand-tracked holdings because no sync of it has arrived since, for longer than the price staleness threshold. Once per account and switch: the payload carries `account` (the path), `since` (the day of the switch) and `stale_days`. Check the connection, or the account's mapping. |
| `recurring.price_changed` | A recurring charge posts at a new amount. |
| `suggestion.created` | A new suggestion lands in the inbox (detected pattern, agent proposal, new-liability scan, a manual transfer confirm proposing a `transfer_link` rule, a match proposal's card — emitted beside `match.proposed` — or a transfer candidate's card, emitted beside `transfer.candidate`); `dedup_key` is its fingerprint. The payload carries `kind` and `source`; a `transfer_link` one also names `account`, `counterparty_account` and `payee`, a `match` one `account`, `label`, `expected_date` and `posted_amount`, a `transfer_pair` one `account` (where the money left), `counterparty_account` (where it arrived) and `amount`. A repeat confirm or re-detection refreshes the open suggestion and emits nothing, and so does a card re-opened because its match was proposed again or its pair is a candidate again. "Not a transfer" on a pair that had no card writes its suggestion rejected, and emits this event and `suggestion.decided`. |
| `suggestion.decided` | A suggestion is accepted, rejected or snoozed; the payload names the `kind`, the decision and, for accepts, what was created: `result_entity_type` `schedule` (active), `rule` (a `transfer_link` rule, still a draft until you activate it), `match`, or `transaction` (a transfer pair's merged transfer). A match card decided in the register (confirm, reject, or marking the bill paid with that row) emits it too, and so does a transfer pair confirmed or marked "not a transfer" in the register; a card Purser withdraws emits nothing. |
| `sync.failed` | A SimpleFIN sync attempt fails. |
| `transaction.posted` | A transaction reaches `posted`. |
| `transfer.candidate` | An amount+window pair looks like a transfer but carries no signal (name, `transfer_link` rule or transfer schedule). Emitted once per pair (`dedup_key` = the pair key of the two posted rows as they were before any correction) by the matching pass that runs after each import and in the nightly job, when both sides are posted and the newer is at most 60 days old — the pairs that get an inbox card; reading `GET /matching/transfer-candidates` emits nothing. Once per pair of bank lines: a later correction of either row, or the posted row that replaces a pending one, does not emit it again. A pair rejected as not a transfer, or unlinked, emits nothing. |
| `transfer.auto_linked` | The deterministic auto-linker merges a pair: exact opposite amounts within the window, plus a signal — a name signal, a `transfer_link` rule declaring the pair (whose `window_days` may set that pair's window, and whose payee scope, when it has one, a leg's payee must match), or a transfer schedule's instance covering it. The payload's `signal` names the first that applies: `rule:<rule id>`, `name` or `schedule:<schedule id>`. |
| `transfer.converted` | A row became a transfer through the `[account]` sugar — your edit, a categorize rule or the categorize pass — without merging with another row: converted in place (a pending row) or replaced by a transfer (a posted row). The payload names the `transfer_id`, the `account` the row was on and the `counterparty_account`; `dedup_key` is the id of the account the row was on. An `[account]` edit that merges two rows or absorbs one emits `transfer.confirmed` or `transfer.absorbed` instead. |
| `transfer.confirmed` | A transfer pair is merged by an explicit confirm. A split loan payment's payload also carries `remainder` (the interest/fee amount) and `remainder_category`; those keys are absent on an ordinary transfer. |
| `transfer.absorbed` | A late-arriving counterpart is absorbed into an existing transfer. |
| `transfer.unlinked` | A transfer link is undone. |
| `valuation.recorded` | A property/loan valuation or holdings snapshot is recorded. |
| `variance.threshold` | A matched pair's amount variance exceeds the threshold. |

### account.reconciled payload

Emitted once, when a reconciliation is recorded. Dry runs and retries emit
nothing. The event's `entity_id` is the account. Amounts are decimal strings in
the ledger's sign (a card is negative when owed), except `statement_balance`,
which is the figure as the statement prints it:

```json
{
  "path": "liabilities.card",
  "currency": "USD",
  "reconciliation_id": "8d1f6c0e-5b7a-4f0e-9c3d-2a6b1e4f7c90",
  "as_of": "2026-09-30",
  "basis": "posted",
  "component": "balance",
  "statement_balance": "425.0000",
  "target_balance": "-425.0000",
  "ledger_balance": "-400.0000",
  "adjustment": "-25.0000",
  "adjustment_tx_id": "c4a2e9b1-7d3f-4e86-b5a0-91f2d6c8e3b7"
}
```

`component` is `cash` for a manual brokerage cash reconciliation and `balance`
for ordinary cash/card accounts. It never claims that brokerage securities
were reconciled.

`ledger_balance` is the balance on `as_of` in that `basis` before the
adjustment. `adjustment` is `"0.0000"` and `adjustment_tx_id` is null for a
clean reconcile. A reconciliation that later goes out of balance emits no
event; reads report it. See [Reconcile an account](../../how-to/reconcile-an-account/).

## Webhooks

Manage subscriptions with admin scope:

- `POST /webhooks` — subscribe a URL to event types (wildcards supported); an
  optional per-subscription secret is encrypted at rest. A new subscription
  receives events from its creation onward — for history, replay
  `GET /events` instead.
- `GET /webhooks`, `DELETE /webhooks/{id}` — list and remove.
- `GET /webhooks/{id}/deliveries` — delivery log with response codes.
- `POST /webhooks/deliver` — process due deliveries immediately (the worker
  also does this on schedule).

## Delivery contract

Each delivery is an HTTP POST of the event JSON with:

| Header | Content |
| --- | --- |
| `X-Purser-Event` | The event type. |
| `X-Purser-Delivery` | Unique delivery id (dedupe on this). |
| `X-Purser-Signature` | Hex HMAC-SHA256 of the raw body, keyed with the subscription secret. Only when a secret is set. |

Verify by recomputing the HMAC over the **raw request body**. Non-2xx
responses retry with exponential backoff — 1, 4, 16, and 64 minutes — then
the delivery is marked failed after 5 attempts. Replay missed events any
time from `GET /events` with your last cursor.
