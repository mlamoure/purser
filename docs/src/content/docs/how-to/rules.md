---
title: Write deterministic rules
description: Categorize, rename payees, and declare transfer pairs with rules that run automatically on new transactions — and never execute until you activate them.
---

Rules are deterministic instructions Purser runs where automation already
touches your rows: at import, and in the nightly sweep. They decide, so they
are deliberately boring — no regex, no scoring, no fuzzy matching. A rule
either matches or it does not, and you can read why off a single line.

There are three kinds:

- **categorize** — set a category, or name another real account to convert the
  row into a transfer, when a rule matches.
- **rename** — map a raw bank descriptor to a friendly payee.
- **transfer_link** — declare that two accounts transfer to each other.

## A rule never runs until you activate it

A rule is created as a **draft** unless the call that creates it sets
`activate`. Drafts are visible, editable and
previewable, but they do not execute — the loader that feeds the import and
sweep paths only ever selects active rules. An agent creates rules as drafts
too, unless you asked it for the rule: then it may create it already active
(`propose_rule` with `activate=true`). A rule that is the agent's own idea
stays a draft for you to decide. A pair (`transfer_link`) rule is always
created as a draft, because its activation confirms how many past pairs it
will link (see below).

Every rule write — creating, activating, editing or reaching back into
history — needs the `write` scope and is recorded in the audit log.

## Preview before you activate

The predicate is the part worth checking, so preview it against your real
history first:

```bash
curl -sX POST https://purser.example.com/rules/preview \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"match_field": "payee", "match_mode": "contains", "match_text": "amazon"}'
```

You get back how many live rows match, how many were scanned, and a sample.
A count far larger than you expected is the signal that the predicate is too
broad — narrow it with an account, an amount range, or a direction.

A `transfer_link` rule previews the same way, with its account pair instead
of a predicate (see [Declaring a transfer pair](#declaring-a-transfer-pair)).

## Matching is token-aware, not substring

Text matching normalizes both sides and respects token boundaries, so a rule
for an account named `Chase` does **not** fire on `PURCHASE RETURN`. Three
modes:

| mode | matches |
| --- | --- |
| `equals` | the whole normalized text |
| `prefix` | starts with the pattern, ending on a token boundary |
| `contains` | the pattern as a whole run of tokens |

`prefix` with `star` matches `star market` but not `starbucks coffee`.
This makes a prefix rule useful for bank descriptors whose stable payee text is
followed by a changing bill number or reference.

## Everything you set is ANDed

A predicate is the AND of whatever fields you set; unset fields are
unconstrained. You can scope by account, by amount (`amount_exact`, or an
`amount_min`/`amount_max` range), by `direction` (`debit` or `credit`), and by
`effective_from`, which excludes rows dated strictly before it.

`direction` reads the amount **after** any per-account sign inversion, so you
write the rule in your sign world, not the bank's.

## Which rule wins

Rules of one kind are evaluated in priority order — lower numbers first — and
the **first match wins**. `GET /rules` returns them in exactly that order, so
resolution is visible before it fires rather than being something you deduce
afterwards.

Priority is a number you set, not something Purser infers from how specific a
rule looks. That is on purpose: a rule's effect should be explainable by
pointing at one row.

## Map a payee durably

For a durable payee mapping, use a `categorize` rule with `match_field: payee`
and choose the narrowest safe text mode. Its action target may be a virtual
category such as `expenses.utilities`, or a real account such as
`liabilities.auto_loan`; a real target uses the same bracket-transfer
conversion as an inline edit.

Rules are the only automatic mapping from payee text that you configure, and
they come first. Payee memory and the AI classifier (next section) only fill in
rows no rule matched, and neither is a mapping you can edit. Nothing in the
suggestions inbox applies a mapping itself either: the most a suggestion can do
is propose one as a rule, which you accept through the same activation as a
rule you write. That includes transfers: confirming a transfer by hand teaches
Purser no hidden payee-to-account mapping — it proposes a `transfer_link` rule
(see [A pair scoped to a payee](#a-pair-scoped-to-a-payee)).

## Rules beat learned memory

For categorization the order is:

**rule, then payee memory, then the AI classifier, then uncategorized**

A rule sits above the payee-to-category memory learned from your accepted
history, so a rule can correct a memory that has gone wrong. If you find
yourself fixing the same categorization repeatedly, that is the signal to
graduate it into a rule.

## Renaming consolidates history, not just new rows

A `rename` rule rewrites the **payee**, never the bank's description — three
different identity checks read that description, so rewriting it would corrupt
them.

Applying a rename to history does not rewrite your transactions. It aliases
the payee itself, pointing the raw payees at the friendly one. Nothing about
any posted row changes, and because payee memory counts rows across the whole
alias group, consolidating several descriptors onto one payee makes that
payee's memory stronger for everything that already happened.

Clearing the alias undoes it.

## Declaring a transfer pair

Purser already links transfers automatically when the text carries a signal —
a keyword like `transfer` or `payment`, or the counterparty account's name.
Some real transfers carry neither. Recurring brokerage moves often arrive as
opaque descriptors on both sides.

A `transfer_link` rule declares the account pair, and for that pair the
name-signal requirement no longer applies. **Everything else still does**:
the amounts must be exactly opposite, the two dates must fall inside the
pair's window, both rows must be posted, and a pair you explicitly unlinked
stays unlinked.

### The pair's date window

By default the two sides of a declared pair must post within 3 days of each
other — the same window every other pair uses. Some transfers take longer: a
card autopay that leaves checking on the 1st and lands on the card on the 5th
is 4 days apart, and would stay unlinked. Set `window_days` on the rule to
give **that pair, and only that pair**, its own window:

```bash
curl -sX POST https://purser.example.com/rules \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type": "transfer_link", "pair_account_a": "CHECKING_ID",
       "pair_account_b": "CARD_ID", "amount_exact": "184.50", "window_days": 5}'
```

- `window_days` is a whole number of days from `0` (same day only) to `14`.
  Leave it out for the standard 3 days. A value outside that range, a
  fraction, or a `window_days` on any other kind of rule is refused with a 422.
- It combines with `amount_exact` and `effective_from`, which narrow the pair
  as before.
- A window below 3 narrows the pair: the rule vouches only for legs that
  post that close together. A name-signal pair still links within the
  standard 3 days, as it would without the rule.
- A wider window never takes a row from a closer pair already in your
  ledger. Each pass first links everything inside the standard 3 days exactly
  as it would without the rule, and only then pairs the declared accounts
  farther apart, among the rows still without a partner. Every pair you did
  not declare keeps the 3-day window.

Keep the window as narrow as the bank's real delay. Exact opposite amounts are
the only other evidence a declared pair needs, and a wide window gives a stray
equal amount more room to pair with the wrong row. A pair that repeats weekly
should not go past 3.

### A pair scoped to a payee

A pair rule can be narrower than the whole pair. When you confirm a transfer
by hand, Purser proposes a `transfer_link` rule for those two accounts that
only applies when one side of a pair carries the confirmed payee — see
[From a confirm to a rule](../../how-to/transfers/#from-a-confirm-to-a-rule).
Accepting the suggestion creates that rule as a draft. Its `predicate` then
reads `a leg's payee is "..." or an alias`, and `GET /rules` and MCP
`list_rules` show the payee as `payee_scope`. Pairs between the same accounts
with any other payee still need a name signal or a confirm.

The scope follows the payee's **aliases**. When a rename's apply-history
points the scope's payee at a friendlier one, or points other descriptors at
it, rows under any payee in that group are covered and the rule shows the
payee under its current name. A rename only merges payees through
apply-history, and apply-history only aliases the payees of live rows. A
payee whose rows have all been merged into transfers is not aliased, and
neither is one whose rename only ever ran on new rows. Rows under the new name
then fall outside the scope; confirm one of those pairs and Purser proposes a
rule for the new name.

The payee scope is part of what an active rule links, like its window: an
active rule refuses an edit that would drop or change it. Rules you write
yourself declare the whole pair; a payee scope comes from an accepted
suggestion.

Preview a pair before you create the rule:

```bash
curl -sX POST https://purser.example.com/rules/preview \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"pair_account_a": "CHECKING_ID", "pair_account_b": "CARD_ID",
       "window_days": 5}'
```

The answer's `links` is how many historical pairs such a rule would link, and
`sample` shows a few of them with their dates and `gap_days`.

### Activating a pair rule

Because the nightly sweep looks at all of your posted history, activating one
of these applies it retroactively. Purser therefore tells you how many
historical pairs it would link — with the rule's own window — and asks you to
confirm that number:

```bash
curl -sX POST https://purser.example.com/rules/$RULE_ID/state \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"state": "active", "confirm_links": 12}'
```

The count is computed by the same selection the sweep then runs, so it is the
number that links: pairs of one-sided rows merged into a transfer, and rows
absorbed into an existing transfer that was waiting for its other side. It
matches what the preview above reported for the same history.

The confirmation is not ceremony. Merging two rows is balance-neutral, so the
post-sync balance check that catches most mistakes cannot see a wrong one —
every balance stays correct while two unrelated rows are silently one. The
count is your check instead. Use `effective_from` to bound it, or narrow
`window_days`, if the number looks high.

An active pair rule cannot be edited in a way that changes what it links (the
pair, `amount_exact`, `effective_from`, `window_days` or its payee scope): the
edit is refused.
Disable the rule, edit it, and activate it again, so a wider scope goes
through the same confirmation.

The **Applied** column on the Rules page (`applied_count` on `GET /rules`,
`applied` in the MCP `list_rules` tool) counts how many times a rule has
fired. A pair rule counts each link it makes, merged pairs and absorbed rows
alike, including the links the nightly sweep makes after you activate it. A
categorize or rename rule counts each new row it shapes, and a run over
history counts once. Previews and the confirmation count above never add to
it, and unlinking a transfer does not take a count back.

## Applying a rule to history

New rules do not quietly rewrite the past. `POST /rules/{id}/apply-history` is
the only path, and it is a dry run unless you say otherwise:

```bash
curl -sX POST https://purser.example.com/rules/$RULE_ID/apply-history \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"dry_run": true}'
```

The dry run's counts are the real run's results — both walk the same candidate
list. That list covers all of your history, however long: the preview samples
the newest 5,000 rows (`scan_capped` says when it stopped there), but
apply-history examines every live row and reports how many as `scanned`.
By default it touches uncategorized rows and rows automation categorized, and
leaves anything **you** categorized alone; overriding your own choices is
opt-in. Posted rows are skipped unless you ask for
`posted: "correct"`, which mints a proper reversal and replacement for each
one.

A categorize rule never touches a transfer, which has no category to set — not
even with `include_user_categorized`. Transfers its predicate matches are left
out of `matched` and reported as `skipped_transfers`. Rows in a different
currency from the rule's category are left out the same way and reported as
`skipped_currency`: a transaction keeps one currency. Split transactions (one
bank amount across several categories, from a QIF import) are read-only and
left out the same way, reported as `skipped_splits`.

`transfer_link` rules have no apply-history: the nightly sweep is their
retroactive application.

## From an agent

Three MCP tools mirror this: `list_rules` shows the active set in evaluation
order, `preview_rule` dry-runs a predicate, and `propose_rule` creates one: a draft,
or an active rule with `activate=true` when you asked the agent for it. There
is no MCP tool to activate a draft later; that happens on the Rules page or
over REST.

For a pair, pass `pair_accounts` (two account paths) to `preview_rule` and
`propose_rule`, with an optional `window_days`. The preview's `links` is the
count the activation will ask you to confirm, and `list_rules` shows each
pair rule's `window_days` and, for a payee-scoped one, its `payee_scope`. An
agent may also accept a transfer-link suggestion with `decide_suggestion`: that
creates the rule as a draft; activating it confirms its projected link count,
on the Rules page or over REST.

## See also

- [Manage transfers](../../how-to/transfers/) — the bracket sugar and manual linking
- [Categories](../../explanation/categories/) — why a category is an account
- [Suggestions inbox](../../how-to/suggestions/) — where a confirm's proposed pair rule waits
- [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
  sections 5 (matching), 7.1 (import order) and 11 (optional AI), plus decisions 46,
  66 and 67
