---
title: Split a transaction
description: Spread one bank row over categories and accounts the way Quicken does — a paycheck's gross pay, taxes, benefits and 401(k) contributions — back account lines with their own downloaded rows, and remove a split.
---

A **split** spreads one bank row over several **lines**. Each line is a
category or another account, and the lines add up to the row's amount. The
commonest case is a paycheck: the bank shows one net deposit, but it is gross
pay minus taxes and benefits, minus the 401(k) contributions that went to the
401(k) account, and sometimes minus the part that landed in a second bank.

The row keeps the bank's exact amount. Each category line counts under its
category in spending reports; each account line is money that went to (or
came from) that account, so it shows in that account's register and is never
spending. A split is never a transfer. For the ledger model behind it see
[Split transactions](../../explanation/ledger/#split-transactions) and
[DESIGN.md §3.3](https://github.com/mlamoure/purser/blob/main/DESIGN.md).

## The lines

A line is `{category, amount, memo, backing_ids}`:

- `category` is a category path (`expenses.taxes.federal`) or an account in
  brackets (`[assets.retirement_401k]`). It can be any category, the
  uncategorized placeholders included, or any other open account — never the
  row's own account.
- `amount` is the line's share of the row's amount, **in the row's sign**:
  negative is money out of the row's account. The lines must add up to the
  row's amount exactly; there must be at least two and at most 50, and none
  may be zero.
- `memo` is optional text for the line, up to 500 characters.
- `backing_ids` (account lines only, at most 50 across the split) — see
  [Back an account line](#back-an-account-line).

A paycheck that deposited `2500.00` into checking:

| Line | Amount |
| --- | --- |
| `income.salary` (gross) | `4000.00` |
| `expenses.taxes` | `-900.00` |
| `[assets.retirement_401k]` | `-600.00` |
| **Total** (the deposit) | `2500.00` |

Checking keeps `+2500.00`; the 401(k) account shows `+600.00`; spending
reports count `900.00` of taxes and `4000.00` of salary.

## Split in the app

In an account register, open a row's **Split…** action. Pick a category or an
account for each line (accounts are in the picker's Transfers group), type
its amount and an optional memo, and add or remove lines. **Left to assign**
shows how much of the row is not on a line yet; **Save split** stays disabled until
it reaches zero, and **Put the rest on Uncategorized** adds what is left as a
line on the uncategorized placeholder, so you can finish it later. A split
row shows **Split (n)** in the category column; clicking it opens the editor
again, where **Remove split** turns it back into an ordinary row after you
confirm (**Keep split** or Escape leaves it as it is).

## Split over the API or MCP

| Surface | Call |
| --- | --- |
| REST | `PUT /transactions/{id}/split` with `{"lines": [...], "dry_run": false}` |
| REST | `DELETE /transactions/{id}/split?category=...` removes it |
| REST | `GET /transactions/{id}/split/candidates?account=...&amount=...` lists backing rows |
| MCP | `split_transaction(transaction_id, lines, dry_run)` and `unsplit_transaction(transaction_id, category, dry_run)` |

Both need the `write` scope, and both are recorded in the audit log and
emitted as `transaction.split` / `transaction.unsplit` events (see
[Events and webhooks](../../reference/events-webhooks/)). `dry_run` checks
everything and writes nothing. The same call replaces the lines of a split
that already exists, including one a QIF import brought in.

```json
{
  "lines": [
    {"category": "income.salary", "amount": "4000.00"},
    {"category": "expenses.taxes", "amount": "-900.00", "memo": "withholding"},
    {"category": "[assets.retirement_401k]", "amount": "-600.00"}
  ]
}
```

A **posted** row is never edited in place: the split is written as a reversal
of the row plus a replacement, and the response's `transaction_id` is the
replacement. The bank's identity stays with the original, so syncing or
re-importing the same rows adds nothing. A pending row you entered yourself
changes in place. The bank's own pending row cannot be split until it posts —
the bank's posted row replaces it. Expected rows, transfers and the cash of a
securities trade cannot be split.

MCP `get_transaction` lists a split's lines in the shape `split_transaction`
takes, so an agent can start an edit from it; `search_transactions` rows
carry `split` (the line count) instead of `category`.

## Back an account line

Your 401(k) feed sends its own row for the contribution. If the split's
`[401k]` line and that row both counted, the 401(k) account would show the
money twice. **Backing** the line with the row merges the row into the split,
the way a confirmed transfer merges its two bank rows: the money counts once,
and the split now carries the bank's row.

- Name the rows in the line's `backing_ids`. Several rows may back one line —
  some 401(k) feeds send one row per fund — and their amounts must add up to
  the line's amount on that account (the line's amount with the sign
  flipped: a `-600.00` line backs with rows totalling `+600.00`).
- A backing row is a single downloaded or imported row on that account — not
  a transfer, not a split, not one that pays a scheduled bill (unmatch it
  first).
- The candidates endpoint, and the editor's checkboxes, list the eligible
  rows within 7 days of the split row, nearest amount first; rows already
  backing the split come first. `split_transaction` lists exact-amount
  candidates for each unbacked account line in its result.
- A line can stay unbacked: its leg is then Purser's own, like a transfer
  you entered by hand. Nothing backs a line automatically — when the
  account's row arrives later, edit the split and back it.
- A pending row may back a line (some feeds send contributions as pending
  rows that never post). If the bank later posts it as a new row, that row
  takes the pending's place in the split at the next sync, so it still
  counts once.

## Remove a split

**Remove split** in the editor, `DELETE /transactions/{id}/split` or MCP
`unsplit_transaction` turns the row back into one category — the one you
give, else the uncategorized placeholder — and brings back every row that
backed an account line exactly as it was, pending or posted. A bank row that
comes back with no category [needs review](../../how-to/review-new-transactions/)
again, as any bank row with no category does. A split that carries backing
rows cannot be voided directly; remove the split first.

## Split like last time

When a new bank row arrives from a payee whose last split on that account
you (or your agent) made, Purser puts a **split** card in the
[suggestions inbox](../../how-to/suggestions/): the same lines, with the
amount difference on the largest category line, and an account line backed
by the one row on that account that exactly matches it, when there is
exactly one. **Split like this** applies it; **Edit…** opens the editor prefilled and
saves through the card; reject remembers your answer for that row, or for
the payee on that account. A payee includes every name a
[rename rule](../../how-to/rules/#renaming-consolidates-history-not-just-new-rows) merged into it, so a payroll your bank
words several ways counts as one payee, for the card and for a rejection. A
card is never applied on its own, and a split a QIF import brought in never
starts one.
