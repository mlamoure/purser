---
title: Matching
description: Why reconciling expectations against imported reality is the center of gravity, and how candidates, tiers, and variance work.
---

Matching is Purser's center of gravity. A ledger that only records what already
cleared is a rear-view mirror; Purser also holds **expectations** — manual bills,
materialized [schedule](../../how-to/schedules-and-bills/) instances, and the
uncleared rows of a [file import](../../how-to/import-qif-csv/) — and
reconciles them against the imported reality of what the bank actually did. That
reconciliation is where "the books cannot be silently wrong" earns its keep.

## Expected versus imported

An `expected` transaction is an estimate: the $60.00 gym charge you know is
coming. An imported `pending` or `posted` row is what the feed reported. The
matcher's job is to recognize that these two are the same event and collapse
them into one truthful row — without ever letting the estimate overwrite what
really happened.

## Candidate scoring

For each expectation the matcher looks at rows in the **same account** within a
date **window** (default plus or minus 5 days), scoring normalized-payee
similarity and amount closeness. Crucially, **amount is weighted softly**: a row
whose payee and date agree but whose amount differs is still a confident match —
it just carries a **variance**. That is deliberate. A subscription that quietly
rose from "-9.99" to "-11.99" should still match its schedule, and the price
change should be *recorded*, not hidden by a failure to match.

Candidates are bank reality: imported rows, and any live **transfer** — the
two-leg row the auto-linker (or your confirm) builds when both sides of a card
payment arrive. Every row is scored **through the account being matched**: a
one-leg import and a two-leg transfer are both "-500.00 on checking" from
checking's point of view. An expected transfer (a schedule with a counterparty
account) is tried from each of its two accounts, and two rows that name the
same counterparty account count as agreeing on the payee — which is how the
expected card payment pairs with a merged transfer that carries no payee.

## Three tiers

The score sorts each expectation into one of three outcomes:

- **auto** — a high score matches automatically.
- **proposed** — a middling score becomes a `proposed` match for you to confirm
  or reject. It waits as a `match` card in the
  [suggestions inbox](../../how-to/suggestions/), and in the account register.
  Rejecting it is remembered: the matcher never proposes — or matches on its
  own — that bank row for that bill again, nor the posted row that replaces
  it. You can still mark the bill paid with it by hand.
- **unmatched** — a low score leaves both rows standing.

## Posted amount wins, and supersede

When a match is made, the expectation is **voided with a `superseded_by` link**
to the posted row, and the posted amount is truth. The flat view collapses the
pair to the single real transaction; the ledger keeps the forensic lineage. The
`matches` row records the score and the variance, and that variance is not
discarded — it sharpens future schedule estimates and, past a threshold, emits a
`variance.threshold` event.

A match can name a pending row, and pending rows are not final. When the feed
replaces a pending with its posted row, a confirmed match moves to the posted
row and a proposal is decided again. When
the bank drops the pending, the match is released and the expectation comes
back, because a dropped charge paid nothing. Voiding a matched pending yourself
works the same way: the match is released (a proposal is withdrawn) and the
expectation is due again. Estimates read only posted rows,
and they read the current version of each: the posted successor of a pending,
the transfer a row was merged into, the replacement of a corrected row.

## Transfers whose sides post on different days

The two bank legs of a transfer rarely post together, and the nightly job
matches before it links transfers. When only the first leg has arrived, an
expected transfer can settle against it — the matcher sees that leg from the
account being matched, so it is a fair candidate (proposed when only the
amount and date agree). That does not lock the leg away. A bank row matched to
an expected transfer stays available for pairing, but only with a row on the
transfer's other account, and when its counterpart arrives and the two link,
the match **follows the bank line** into the merged transfer: a confirmed
match simply moves, and a proposal is decided again against the merge, which
agrees on accounts, amount and date. The expectation is settled exactly once,
by the transfer, and the merged transfer — now matched — settles nothing
else. A row matched to an ordinary bill never takes part. Unlinking the
transfer releases what it settled, so the expectation is due again rather
than settled by a row that no longer exists.

## Unmatched, both directions

The gaps are reported both ways, with aging: **expected-but-missing** rows are
late bills, and **posted-but-unexpected** rows are new spending to triage. The
`list_unmatched` tool answers each direction, so an agent can ask "what bills
haven't shown up?" and "what did I spend that I didn't plan?" as one call each.

Transfer detection is a specialized branch of the same machinery — see
[Manage transfers](../../how-to/transfers/). The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 5, and the principle behind it is design principle 5.

Correction history contributes only its current replacement. A reversal is an
accounting entry, not a bank payment, and cannot settle an expected bill.
