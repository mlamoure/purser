---
title: Suggestions inbox
description: Decide what Purser thinks you should do — detected recurring bills and transfers, agent-proposed schedules, transfer-link rules your confirms propose, bill matches the matcher proposes — from the Dashboard cards or the MCP tools.
---

A **suggestion** is something Purser thinks you should decide: a recurring
payee it found in your history, a schedule an agent proposed, a payment for a
card you just added, a rule to link a transfer you keep confirming by hand, or
a bank row the matcher thinks paid one of your expected bills.
It carries a typed **draft** of what it would create, the **evidence** behind
it, a **confidence** where Purser scored one, and it remembers what you
decided. The Dashboard cards and the MCP tools are two renderings of the same
rows.

Every suggestion has a `kind`, and the kind decides what its draft is:

| Kind | Draft | Accepting it creates |
| --- | --- | --- |
| `schedule` | a schedule (name, account, rrule, amount, ...) | an **active** schedule, materialized immediately |
| `transfer_link` | a `transfer_link` [rule](../../how-to/rules/#a-pair-scoped-to-a-payee) for one account pair, scoped to a payee | the rule as a **draft**, which links nothing until you activate it |
| `match` | an expected row and the bank row the matcher proposed for it, with both amounts and the variance | the confirmed **match**: the bill is paid by that row |

## Where suggestions come from

- **Detection.** The nightly detector (07:05 UTC), `POST /schedules/detect`
  or MCP `detect_schedules` scans your history per account and counterparty —
  payees for ordinary bills, the other account for transfers — for recurring
  cadences (weekly to yearly, including semi-monthly paydays). Each pattern
  becomes one suggestion — never a schedule — with a confidence that reflects
  how much evidence there is, how regular the gaps are and how steady the
  amount is. Running detection again refreshes the open row (its `seen_count`
  climbs) instead of creating a duplicate.
- **Agents.** A `create_schedule` call with `propose=true` lands here with the draft
  the agent asked for, `source = agent`. It has no confidence, because
  nothing scored it, so it lists after the scored suggestions. The agent can
  attach a `note` of up to 200 characters, such as the statement line it read,
  and the card shows it. If the account already has a schedule for that payee
  or counterparty (active, paused or ended), the proposal is refused with a
  `422` that names the schedule, so the inbox never holds a duplicate of it.
- **A new liability account** (card, loan) triggers a scan for payments to it
  — merged transfers to the account and one-sided outflows whose payee names
  it — and lands them as `source = account_added`.
- **A manual transfer confirm.** When you merge two bank rows with
  `confirm_transfer` (MCP) or `POST /matching/confirm-transfer`, Purser
  proposes a `transfer_link` rule for that account pair, scoped to the payee
  on the rows, as `source = confirm`. Confirming another pair of the same
  accounts and payee refreshes the same suggestion — its evidence is the
  pairs you confirmed — instead of adding one. A split loan payment proposes
  nothing, and neither does a pair whose rows carry no payee. See
  [Manage transfers](../../how-to/transfers/#from-a-confirm-to-a-rule).
- **The matcher.** When the nightly matching pass (or `POST /matching/run`)
  scores a bank row in the middle tier for an expected bill — close, but not
  sure enough to match on its own — it proposes the match and the proposal
  appears here as a `match` card (`source = detected`, the match score as
  its confidence). The card only mirrors the proposal: confirming or
  rejecting it in an account register, or marking the bill paid with that
  row, decides the card too, and when the proposal goes away on its own —
  the bank drops the pending row, the row merges into a transfer, a better
  row settles the bill — the card is **withdrawn**. A bank row matched to an
  expected transfer waits a few days for its other leg before it gets a card,
  because the transfer usually settles it by itself. See
  [How matching works](../../explanation/matching/).

## Deciding

`POST /suggestions/{id}/decide` or MCP `decide_suggestion`, write scope:

| Decision | Effect |
| --- | --- |
| `accept` | Applies the draft exactly as shown. A `schedule` becomes an **active** schedule, materialized immediately; a `transfer_link` becomes a **draft** rule (below); a `match` confirms the match, which also marks the bank row reviewed. |
| `edit_then_accept` | Same, after merging your `edits`. A schedule takes any draft field — `name`, `rrule`, `dtstart`, `amount`, `amount_type`, `estimate_basis`, `payee`, `category`, `counterparty_account`, `autopost_policy`. A transfer link takes only what narrows or widens it — `amount_exact` (a decimal string), `window_days` (0 to 14) and `effective_from` — because its account pair and payee are what it proposes. A match has nothing to edit: to pay the bill with a different row, mark it paid with that row (`POST /matching/match`, MCP `match`). The edits are validated by the same schema before anything is created. |
| `reject` | Durable. `reject_scope = counterparty` means "never suggest this payee or counterparty on this account again" — for a transfer link, never again for this account pair, whatever the payee; `exact` (default) rejects just this pattern, or this pair and payee. A match is rejected exactly: the matcher never proposes that bank row for that bill again, nor the posted row that replaces it when it was pending. |
| `snooze` | Hides it until `snooze_until` (default 30 days). Nothing auto-accepts on expiry — it simply returns to the inbox. A snoozed match stays proposed: its bank row is still held, and the forecast still counts the bill as paid pending your confirmation. |

Deciding a match card whose proposal is already gone answers 422 and
withdraws the card.

### Accepting a transfer link

A transfer-link suggestion never links anything by itself, and accepting it
does not either. Accepting creates the rule as a **draft** and returns
`projected_links`: how many of your past pairs the rule would link if you
activated it now. Activation is the rule's own confirm step — the Dashboard
card asks it right after you accept, the Rules page offers it for every draft,
and over REST it is `POST /rules/{rule_id}/state` with
`{"state": "active", "confirm_links": N}`. An agent can accept with
`decide_suggestion`, but there is no MCP tool to activate the rule: the count
confirm happens on the Dashboard card, the Rules page or over REST. See
[Activating a pair rule](../../how-to/rules/#activating-a-pair-rule) for why the
count is asked for.

On the Dashboard each open suggestion is a card: the headline, its source and
confidence, an evidence summary that expands into the occurrences (or, for a
transfer link, the pairs you confirmed), and the four decisions (rejecting
asks whether to reject just this one or never suggest the pair again).
`GET /suggestions` lists the open inbox by confidence — `?kind=schedule`,
`?kind=transfer_link` or `?kind=match` narrows it, and `?status=withdrawn`
shows the cards Purser closed on its own; `GET /suggestions/{id}` adds the
full evidence. A match card shows the expected row against the bank row and
the variance, with Accept, Snooze and Reject. MCP `list_suggestions` (with an optional `kind`) returns the same
rows with a one-line `headline`, the draft, an evidence summary (count,
first/last, amount range, cadence, and an agent's `note`) and the confidence; `whats_new` carries the
open count and the top few.

## Why it remembers

Every suggestion has a stable **fingerprint** — the account it lives on plus
who it is with (a payee, or a counterparty account); for a transfer link, the
two accounts in either order plus the payee. One row per fingerprint, ever: a
pattern found tomorrow lands on the same row, a pattern you rejected last week
stays rejected, and a pattern already covered by one of your schedules — or a
pair and payee one of your `transfer_link` rules already covers — is never
suggested at all. That is what keeps the inbox from becoming noise. A match's
fingerprint is the bill's schedule slot (or the expected row) plus the bank
row, so rejecting it holds even when the bill comes back as a fresh copy in
the same slot.

A transfer link's payee is matched through its **aliases**: if a rename rule
later merges that payee into a friendlier one, a confirm under the new name
still refreshes the same suggestion, and a rejection still holds.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 6 (schedules and forecasting) and decisions 57, 67 and 86. Schedules
themselves are covered in
[Schedules and recurring bills](../../how-to/schedules-and-bills/), rules in
[Write deterministic rules](../../how-to/rules/).
