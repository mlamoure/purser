---
title: Transactions that need review
description: Purser flags a transaction only when there is something to fix — a bank-sync row with no category, or a reconciliation adjustment — and the flag clears when you fix it.
---

Purser flags a transaction as **needs review** only when there is a reason,
and it tells you which:

- **No category.** A row the bank feed (SimpleFIN) brought in has no
  category: `expenses.uncategorized` or `income.uncategorized`, on the row or
  on a line of a split.
- **Reconciliation adjustment.** The difference Purser posted when you
  [reconciled an account](../../how-to/reconcile-an-account/) and your
  statement did not agree with the ledger.

Nothing else is ever flagged. Rows you type in, rows your agent writes, QIF
and CSV files you import, starting balances, brokerage balance adjustments,
changes in an asset's value, merged transfers and upcoming rows a schedule
expects never need review. Who or what wrote a row does not matter either: a
bank row your rule or Purser's payee memory already filed is as done as one
you filed yourself.

The flag is not a mark someone sets. Purser reads it from the row every time,
so it clears the moment the reason is gone, and nothing needs to be marked
as reviewed.

## Rows with no category

A bank-sync row needs review while it has no category. It clears as soon as
anything gives it one: a rule, Purser's memory of the payee, the AI
categorizer, you, or your agent. A split clears when none of its lines is
uncategorized.

The flag follows the bank line, not the row id. If you edit a posted bank row
(its description, say) without giving it a category, the corrected row still
needs review; once the bank posts a pending row, the pending row drops out and
the posted row is the one that counts.

When you file the same payee over and over, write a rule so it never comes
back: see [Write deterministic rules](../../how-to/rules/).

You can turn this reason off on the **Categories** tab of **Settings ->
Categories** (the `review_uncategorized` setting,
`PURSER_REVIEW_UNCATEGORIZED`). Rows with no category then stay
uncategorized without a flag; reconciliation adjustments are still flagged.

If you use the AI categorizer, its confidence floor decides how sure it must
be before it files a row (`ai_min_confidence`, 0.7 by default; **Minimum
confidence** on the **AI categorization** tab of **Settings -> Categories**,
as a percentage). A guess below the floor is not applied: the row stays
uncategorized, so it needs review. See
[Configuration](../../reference/configuration/).

## Reconciliation adjustments

When a reconcile posts an adjustment to
`expenses.reconciliation_adjustments`, that row needs review until you deal
with it in one of two ways:

- **Explain it.** If you find what the difference was (a fee, a missed
  deposit), recategorize the row, or split it into what it really was. Once
  no part of it is on `expenses.reconciliation_adjustments`, it no longer
  needs review.
- **Accept it.** If the difference is right as it is, accept it: in the web
  app, **Accept…** on the row in the account's register. The
  acceptance is recorded on the reconciliation, with who accepted it and when,
  and in the audit log (`reconciliation.adjustment_accepted`), with your note
  if you give one.

A change in a hand-valued asset's value (a reconcile of a property account) is
never flagged.

## Finding them

In the web app, a row that needs review carries a badge in its register that
says why (**needs review · no category** or **needs review · reconciliation
adjustment**), and **Needs review only** narrows the register to those rows.
The dashboard's **Needs attention** counts them by reason and account, and
links each account to its register narrowed that way.

Over the REST API:

| What | How |
| --- | --- |
| Is this row flagged, and why | `needs_review` and `review_reason` (`uncategorized` or `reconciliation_adjustment`, else null) on every row from `GET /transactions` and `GET /transactions/{id}` |
| Only the flagged rows | `GET /transactions?needs_review=true` (`false` is every other row) |
| The same in a search | `is:needs-review` (see [Search transactions](../../how-to/search-transactions/)) |
| Counts by reason and account | `GET /transactions/review-summary` |
| The rows with what it takes to fix them | `GET /transactions/review-batch` |
| Accept an adjustment | `POST /transactions/{id}/accept-adjustment`, optionally with `{"note": "..."}` (write scope; repeating it changes nothing) |

Read `needs_review` rather than working it out yourself: the server applies the
one rule above, and every count uses the same rule.

## With an agent

`describe` and `whats_new` say how many transactions need review and why.
`review_batch` lists them, newest first, each with its reason, account, payee,
description, amount, category and the financial notes that apply. Your agent
categorizes a row with no category with `update_transaction` (and proposes a
rule for a payee that keeps coming back), and either recategorizes, splits or
accepts a reconciliation adjustment (`accept_reconciliation_adjustment`).
Nothing needs to be marked afterwards: a fixed row drops off the list.

## See also

- [Reconcile an account](../../how-to/reconcile-an-account/)
- [Write deterministic rules](../../how-to/rules/)
- [Use a local model](../../how-to/local-model/)
- [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
  section 4 (transactions that need review) and decision 141
