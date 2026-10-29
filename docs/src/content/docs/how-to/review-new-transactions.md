---
title: Review new transactions
description: Rows that arrive without human eyes form an unreviewed set over your registers — how to work through it yourself, with an agent, or automatically.
---

Some rows land in your ledger without anyone looking at them: transactions the
SimpleFIN feed imports, and transfers Purser merges on its own. Those form the
**unreviewed set**.

It is a filter over your existing registers, not a second inbox. Purser
deliberately replaced its review queue with per-account registers, and this
does not bring the queue back — a row is simply flagged until something looks
at it.

## What is in the set

Only rows that arrived unattended:

- transactions imported from the **feed**;
- transfers Purser **merged automatically**;
- replacements produced by a **rule**;
- adjustments Purser posts when you
  [reconcile an account](../../how-to/reconcile-an-account/), whoever asked for
  the reconcile.

Your own imports are not included. A QIF or CSV file you chose to import is
not something you need reminding to review, so a 6,000-row Quicken import adds
nothing to the set. Neither do rows your agent writes: each is attributed to its
token in the audit log and marked agent-made in the register.

Upcoming rows a schedule expects are not in it either. An expected bill or
paycheck is a forecast, not something that happened; when the bank row it
predicted arrives, that row is the one to review.

Everything that existed before this feature shipped counts as reviewed, so it
starts quiet and only ever concerns rows that arrive from now on.

## Rows leave the set on their own

You usually do not have to mark anything. A row clears when **anything you or
your agent does touches it**: editing it, posting it, confirming a transfer,
confirming a match or matching it to an expected row by hand. The action *is*
the review.

Automation is different. A rule that recategorizes a row, the nightly
categorization pass, or a match Purser confirms on its own leaves it needing
review — automation touching a row is not the same as someone checking it.
That is the whole point of the flag.

A review **follows the bank line**. When a pending row you reviewed (or
confirmed a match on) posts, the posted row arrives already reviewed — who
reviewed it, when, the note and any flag come along — as long as it carries the
same category you saw on the pending. If the posted row lands in a different
category, it needs review again, because that category is something nobody has
looked at. A row the bank posts after you never reviewed its pending stays in
the set. A merged transfer is a new judgement of its own, so merging rows into
a transfer never carries their review.

## Working through it

In the UI, unreviewed rows carry a `needs review` badge in their register, and
the dashboard shows how many are outstanding. Both come from the same
server-side check, so they always agree. Over the REST API, each row from
`GET /transactions` (and a single transaction's detail) carries it as
`needs_review`; read that rather than treating a missing `reviewed_at` as
unreviewed, because rows outside the set never get a mark.

From an agent, `review_batch` returns the next rows **with everything needed
to judge them inline** — current category and who chose it, any transfer on
the row, the raw-versus-friendly payee, what automation did — plus a short
checklist. There are no per-row round-trips by design; a reviewer that has to
fetch context row by row will skip it.

Then `mark_reviewed` closes the loop, in bulk and idempotently.

### Check what context was available

Every batch reports a `context` map saying which surfaces it could actually
offer. If something a decision needed was unavailable, that batch's rows were
reviewed with less information than they look like they had — so the guidance
says to **escalate rather than clear** in that case.

### Escalating

A reviewer that is unsure can mark a row reviewed **and** flag it for you.
The row leaves the unreviewed set — no second queue appears — but shows a
`flagged` badge and carries a note explaining why.

The clearest case is an ambiguous transfer: categorizing a row as
`[account path]` names an account, not a specific row, so when two rows in
that account could be its pair, the right move is to hand it back rather than
guess.

## Letting Purser review it for you

If you have configured an AI provider, you can have the review pass run
server-side instead of waiting for a connected agent. It is off by default and
needs its own switch — configuring a provider grants nothing on its own.

When enabled, it runs after each sync, clears what the context supports, and
escalates the rest, so your attention lands on the residue rather than the
whole set. It is capped per run, and a provider failure simply leaves rows
unreviewed rather than breaking the sync.

Rows it clears are recorded as `ai_review`, permanently distinguishable in the
audit trail from a row you looked at yourself.

## Nothing grows forever

If reviewing lapses, rows older than `PURSER_REVIEW_MAX_AGE_DAYS` (90 by
default) are auto-marked as `aged` by a nightly sweep. That is deliberately a
different mark from a real review, so an audit reader can always tell which
rows were actually examined — and the count is reported in your digest rather
than clearing silently, because the sweep firing at all means review has
lapsed.

## Fixes that repeat should become rules

The checklist ends with this and it is worth repeating: if you find yourself
making the same correction over and over, that is a rule waiting to be
written. These flags are the record of what your rules did **not** catch.

See [Write deterministic rules](../../how-to/rules/).

## See also

- [Write deterministic rules](../../how-to/rules/)
- [Manage transfers](../../how-to/transfers/)
- [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
  section 4 (review marks) and decision 47
