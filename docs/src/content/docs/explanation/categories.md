---
title: Categories
description: Why categories are hierarchical virtual accounts rather than tags, how the curated default set works, and how imports arrive already categorized.
---

In Purser a category is not a label you stick on a transaction. It is an
**account** — a virtual one — and categorizing is a real posting to it. That one
decision is why spending rollups, transfers, and double-entry all compose cleanly
instead of fighting each other.

## Categories are virtual accounts

There is no categories table. A category is an `accounts` row with
`is_virtual = true`, an income or expense type, and its place in a hierarchy
given by an `ltree` path like `expenses.dining.coffee`. Because the hierarchy is
an ltree, a spending rollup is a single indexed subtree query — everything under
`expenses.dining` is `path <@ 'expenses.dining'`, one query, no recursion in
application code.

A path is labels joined by single dots, each label made of letters, digits,
`_` and `-`. A malformed path such as `expenses..dining` or
`expenses.eating out` is rejected with a 422 whose message names the
offending segment, so a typo never reaches the database.

## Why this beats tags

A tag is metadata bolted onto a row; nothing balances, and a subtotal means
scanning and summing. Making the category an account means the
[flat write](../../explanation/ledger/) `{ "category": "expenses.dining" }` expands to
a balanced posting pair, subtree totals fall out of the ledger itself, and a
transfer is just a transaction whose "category" happens to be another **real**
account. Categorization becomes a first-class ledger operation with an audit
trail, not an editable sticky note.

## A curated, described default set

Purser ships 100 categories whose structure is adapted from the Plaid
Personal Finance Categories taxonomy, with the transfer and loan-payment groups
deliberately removed — those are structural transfers here, not spending. The
set is seeded once and merges additively on upgrade, so your deletions stick.
Every category carries a one-line **description**, in Purser's own words,
written for the AI classifier and surfaced in the **Config -> Categories** tab.

The set is **finite by design**. You may rename, re-describe, and add categories,
and delete any that are unused, but re-parenting and path renames are not
supported in v1. The AI classifier picks from a closed list — the existing tree
plus your own accounts in brackets (see below) — and can never invent a
category. Agents have no category-creation tool, and creating a category is
an admin-UI action, so the taxonomy stays curated rather than sprawling into a
thousand near-duplicates.

Category paths stay put — schedule templates store them — but a **real**
account can move. `PATCH /accounts/{id}` with a new `path` moves it and every
account beneath it, keeping their relative paths: a card created at
`citicard` becomes `liabilities.citicard`. History and balances follow,
because transactions point at accounts by id. A destination that another
account already occupies is a 409. A destination under a category root such
as `expenses` is refused, because subtree totals there would count the
account's own postings as spending.

Renaming a real account, in the app or with the MCP `update_account` tool,
changes only its display name and keeps its path, so tools, rules and
outside integrations that refer to it by path keep working. The name still
matters to matching: a bank row that mentions an account's name is one of
the signals that links a transfer to it.

## Born categorized

Imports are not dumped in as uncategorized. A deterministic payee-to-category
memory, learned from your accepted history, runs **at import time**: when Purser
has seen a payee before, the new row arrives already categorized, and no
correction is ever needed. The optional [AI classifier](../../reference/configuration/)
handles only the misses. See design section 11 for that pipeline.

## The bracket duality

Because a category *is* an account, the "category" slot is polymorphic. Normally
it holds a virtual category account. On a transfer it holds the **other real
account**, shown in Quicken-style brackets like `[assets.savings]`. Same slot,
same flat shape — a categorized expense and a transfer differ only in whether
that slot points at a virtual or a real account. See
[Manage transfers](../../how-to/transfers/) for the write-side sugar.

The optional AI classifier can use that slot too: your own accounts are offered
to it as bracket options, so a transfer has a correct answer available instead
of forcing a wrong category. A bracket answer is **one-sided** — it names the
other account and leaves that side unbacked until the counterparty row arrives —
and it must clear a higher confidence bar than an ordinary category, because it
restructures the row and moves a second account's balance. Below that bar the
row simply stays uncategorized. Collapsing two rows that describe the same
movement stays deterministic and is never something the model decides.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
sections 3.2 (categories as virtual accounts), 3.3 (bracket transfers), and 11
(AI categorization).
