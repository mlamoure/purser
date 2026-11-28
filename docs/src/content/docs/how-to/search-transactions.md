---
title: Search transactions
description: One search syntax for the register's search box, the REST q parameter and the agent's search_transactions tool — words, phrases, field scopes, amounts, dates and statuses.
---

The search box above every register, the **Search** page (every account at
once, with the search in the address so you can bookmark it), the `q` parameter
of `GET /transactions` and the `q` argument of the MCP tool
`search_transactions` all read the same syntax. Every part of a search must match; there is no `OR`
(run two searches instead).

## Words and phrases

| Search | Finds |
| --- | --- |
| `coffee` | rows whose payee, description or memo contains "coffee" |
| `star` | "Starbucks" too: a word matches anywhere inside the text, as you type |
| `whole foods` | rows containing both words, in any field and any order |
| `"whole foods"` | the exact phrase, in one field |
| `-refund` | leaves out the rows that match |

Case, accents and punctuation are ignored: `cafe` finds "Café", and `7-eleven`
and `7 eleven` are the same search. Letters and digits of every script are
kept, so a search in any language works.

## Typos

Searches are exact, so a count never changes behind your back. Ask for typo
tolerance on one word with a tilde: `starbuks~` also finds words one typo away
("starbucks": a letter missing, extra or wrong, or two letters swapped). It
works on words of 3 to 32 letters without digits, alone or after a field
(`payee:costko~`).

When a search of a single word finds nothing, Purser may answer "Did you mean
…?" with the query rewritten to a word one typo away that does occur in your
transactions. Agents receive it as `did_you_mean`, except when they withhold
payees or descriptions, because the suggestion is a word from your own text.

A search can be up to 1,000 characters and 40 terms long; a longer one is
refused with a message saying so.

## Look in one field

| Search | Finds |
| --- | --- |
| `payee:costco` | the payee only |
| `desc:"card purchase"` | the description only (`description:` works too) |
| `memo:lunch` | the posting memos only |
| `account:checking` | rows on an account whose name or path contains "checking" |
| `category:dining` | rows in a category whose name or path contains "dining", subcategories included |

Put a minus in front of any of them to exclude instead: `-payee:costco`,
`-category:groceries`.

A word with a colon that is not one of these fields is ordinary text, because
bank descriptions often contain colons (`REF:12345`). A field name one letter
off, such as `paye:`, is answered with an error that names the fields; put it
in quotes (`"paye:x"`) to search the text itself.

## Amounts, dates, statuses

| Search | Finds |
| --- | --- |
| `amount:42.10` | rows whose amount is 42.10, money in or out |
| `amount:>100`, `amount:<=20` | larger than 100, at most 20 |
| `amount:10..20` | from 10 to 20 (also `amount:10..` and `amount:..20`) |
| `is:debit`, `is:credit` | money out, money in |
| `date:2026` | a year; `date:2026-10` a month; `date:2026-10-05` a day |
| `date:>=2026-01-01` | from that day on (also `>`, `<`, `<=`) |
| `date:2026-01..2026-03` | from the start of January to the end of March |
| `status:pending` | one status, or several: `status:pending,posted` |
| `is:needs-review` | rows that [need review](../../how-to/review-new-transactions/): a bank-sync row with no category, or a reconciliation adjustment |

`amount:` compares the size of the amount, so it takes no minus sign; combine
it with `is:debit` or `is:credit` for the direction. In a register (or with
the `account` parameter) the amount is that account's side of the row; a
transfer searched across all accounts counts as both money out and money in.
Amounts may be written with a currency sign and thousands separators
(`amount:$1,200`).

Without a `status:` the register shows pending and posted rows; `status:` and
the register's status menu both apply. A search's date range also combines
with the date fields beside the box.

## Examples

- `blue cup amount:<10 date:2026-01`: small Blue Cup purchases in January 2026.
- `category:groceries -payee:"corner market" is:debit`: grocery spending
  anywhere but one store.
- `memo:reimbursable status:posted`: posted rows you marked reimbursable.

## For agents

`search_transactions` takes the same `q`, with `account`, `category`, `start`,
`end` and `status` as separate arguments that combine with it. When payees are
withheld, the tool returns references such as `py_8k2mq4x7ra`: search by one
with `payee:py_8k2mq4x7ra` (or the reference on its own) to get exactly that
payee's rows. An unknown reference, or a malformed clause, is a tool error
that says how to write it. A one-word search that finds nothing may carry
`did_you_mean` (the query with a word one typo away that does occur); retry
with it, or add `~` to the word. It is left out when the call withholds payees
or descriptions. Totals stay exact; for sums use `spending_summary`, not the
rows.

Searching never changes what a response contains: withheld fields stay
withheld (see [Privacy and agents](../../explanation/privacy-and-agents/)).

## How it stays fast

Descriptions, memos and payee names are encrypted in the database, so the
server keeps a decrypted copy of them in its memory, never on disk, and finds
matches there before the database applies the other filters. See
[Encryption at rest](../../explanation/encryption-at-rest/#searching-and-deduplicating-encrypted-data).
