---
title: The ledger
description: Flat outside, double-entry inside — why the books cannot be silently wrong.
---

Purser's core promise is that the books cannot be silently wrong. Three
mechanisms deliver it: a double-entry core behind a flat API, immutable
posted rows with linked corrections, and an append-only raw feed the ledger
can always be re-derived from.

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

One bank amount spread over several categories is a **split transaction**:
one leg on the real account plus one leg per category line, still summing to
zero. It is never a transfer and never has a second real leg; money a split
sends to another account is a transfer of its own. The flat view shows a
split with no single `category_path` and a `split_lines` count, and the
transaction's postings carry the lines. Split rows come from
[QIF imports](../../how-to/import-qif-csv/#split-transactions) today and are
read-only.

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

Nothing is ever silently edited or deleted; every correction is itself a
visible ledger event with an audit trail.

## The raw feed underneath

Every payload a bank connection returns is archived verbatim, append-only,
**before** anything touches the ledger. Feed syncs and manual imports are
content-hash idempotent — re-running any import produces zero new rows. A row's
identity is what the bank sent (account, date, amount, description) plus, for
identical rows in one payload, which occurrence it is — so two same-day coffees
at the same price stay two rows, and re-fetching or re-importing them adds
none. Every ledger row traces to the source bytes that produced it, and the
ledger is a projection that can be re-derived from the archive.

## Where the invariants live

These rules are enforced structurally — app-layer checks with database
triggers as backstop, and property-based tests over the invariants
(zero-sum postings, immutability, idempotent import). The authoritative
specification is [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
in the repository — these pages explain the behavior; that document defines it.
