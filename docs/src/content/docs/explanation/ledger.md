---
title: The ledger
description: Balanced postings, correction history, source records and reconciliation.
---

Purser checks that transaction postings balance, preserves the history of
corrections and archives imported source data. Reconciliation compares ledger
balances with statements. These checks detect inconsistencies; they do not
guarantee that source data or categorization is correct.

## Flat outside, double-entry inside

Clients — human or agent — submit the flat form a person thinks in:

```json
{ "account": "citicard", "date": "2026-07-01", "amount": "-14.00",
  "payee": "Chipotle", "category": "expenses.dining" }
```

The server expands every write into a balanced posting pair (`citicard`
-14.00, `expenses.dining` +14.00). Postings per transaction sum to zero per
currency — enforced in the application layer with a database trigger as
backstop. Reads return the flat view by default; full postings are available
on request.

Categories are themselves accounts — hierarchical virtual accounts with
ltree paths like `expenses.dining.coffee` — which is what makes
categorization a first-class ledger operation instead of a tag. A transfer
is one transaction with two real-account postings; in the flat view its
"category" appears as the other account in Quicken-style brackets, like
`[assets.savings]`. A split loan payment keeps both bank legs exact and adds
one interest/fee posting for the difference, so it still sums to zero — see
[Confirm a split loan payment](../../how-to/transfers/#confirm-a-split-loan-payment).

### Split transactions

One bank amount spread over several categories and accounts is a **split
transaction**, as in Quicken: the bank's leg keeps its exact amount, and each
line is a category or another account (`[assets.retirement]`) whose amounts
add up to it. A category line books its share to the category; an account
line books it as a leg on that account. The row still sums to zero, and it is
never a transfer, whatever its legs look like. An account line can be backed
by that account's own downloaded row, which merges into the split so the
money is counted once. The flat view shows a split with no single
`category_path`, a `split_lines` count and `split_account_path`; the detail
lists its lines. See [Split a transaction](../../how-to/split-transactions/).

## Lifecycle and immutability

Every transaction moves through `expected -> pending -> posted -> void`:

- **expected** — future entries: manual bills, materialized schedule
  instances, and the rows a [QIF or CSV file](../../how-to/import-qif-csv/) marks
  uncleared. Estimates, consumed by the matcher and forecaster.
- **pending** — rows the bank feed reports as not yet cleared, and manual or
  agent entries the bank has not posted yet. Freely editable.
- **posted** — **immutable**. Any change, including recategorization,
  executes as an atomic reversal + replacement pair linked via
  `reverses_id`. The flat view collapses the chain; the ledger keeps the
  forensic history. The database allows exactly one mutation of a posted
  row: voiding it with a lineage link in the same statement.
- **void** — dead rows kept for lineage: superseded expecteds, merged
  transfer halves, vanished pendings, reversal legs.

Corrections to posted transactions are recorded as ledger events with an
audit trail.

In the admin app's register, changing a posted row's category is just that:
there is no confirmation step. The corrected row shows **Corrected · Undo**,
and Undo puts the previous category back the only way a posted row allows, as
one more reversal + replacement. Both corrections stay in the ledger and the
audit log.

## The raw feed underneath

Every payload a bank connection returns is archived verbatim, append-only,
**before** anything touches the ledger. Feed syncs and manual imports are
content-hash idempotent — re-running any import produces zero new rows. A row's
identity is what the bank sent (account, date, amount, description) plus, for
identical rows in one payload, which occurrence it is — so two same-day coffees
at the same price stay two rows, and re-fetching or re-importing them adds
none. Imported rows retain links to their source records. Manual entries and
later corrections have their own audit history; bank payloads alone do not
contain those decisions.

## Where the invariants live

These rules are enforced structurally — app-layer checks with database
triggers as backstop, and property-based tests over the invariants
(zero-sum postings, immutability, idempotent import). The authoritative
specification is [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
in the repository — these pages explain the behavior; that document defines it.
