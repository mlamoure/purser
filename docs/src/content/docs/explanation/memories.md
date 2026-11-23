---
title: Financial notes
description: Save notes about your finances in Purser for your agent to use as context.
---

Financial notes keep information alongside your records that transactions alone
cannot explain: "the HOA bill is annual," "this transfer is a 529 contribution,"
or "pay this card from checking." Your agent can retrieve these notes as context
in later sessions.

The API calls these records **memories**. The existing `/memories` endpoints,
`list_memories`, `propose_memory` and `archive_memory` tools manage them. In the
admin UI, they appear under **Settings → Agent memory**, where you record a note and
tick the accounts it is about (none ticked: it applies everywhere).

## Notes and rules

Notes store context; they are not executable rules. Saving a note does not
change a ledger entry. A connected agent, or an optional model integration,
can use the text when deciding what to do.

For automatic categorization or payee renaming, use a
[rule](../../how-to/rules/). Rules can cite a note, but archiving that note
does not disable the rule.

## Where they show up

- **`describe()`** returns the active-note count and a bounded set of pinned notes.
- **`list_memories`** retrieves notes, optionally filtered by account, category or payee.
- **The review loop** attaches notes relevant to the transaction being reviewed.
- **Optional AI categorization**, when configured, receives relevant notes as prompt context.

## Relevance

Notes can link to accounts, categories or payees. Filtering uses those exact
links. Notes with no links apply globally.

At categorization time, the category is still being decided, so the classifier
uses account and payee links. A note linked only to a category will not reach
it. Link to the relevant account or payee when you want it used there.

## Drafts and activation

When an agent proposes a note, it starts as a draft unless you explicitly
asked the agent to save it as active context. You can approve drafts in
**Settings → Agent memory**. Drafts are excluded from `describe()`, ordinary listings,
review context and classifier prompts. Listings can request them explicitly
with `include_drafts` for inspection.

Purser allows up to 500 characters per note, 100 active notes, 8 pinned notes
and 25 drafts.

## Response limits

`describe()` includes at most 8 pinned notes and 1,000 characters.
`list_memories` returns at most 25 notes and 2,500 characters per call.
Classifier context includes at most 5 notes. Listing responses report omitted
counts when a limit truncates the result.

## Privacy

Note text is encrypted at rest. Reading notes requires `read` scope; creating
or updating them requires `write`. A call that withholds payees
(`withhold: ["payee"]`) gets known payee names in the note text replaced with
references. See
[Privacy and agents](../../explanation/privacy-and-agents/).

## Archiving

Archiving removes a note from active context and can be reversed. Hard deletion
requires admin access. Its audit record remains only for the configured
retention period.

Purser does not automatically edit or archive notes. If all of a note's linked
accounts are closed or hidden, Purser flags it as stale for review.

## See also

- [Write deterministic rules](../../how-to/rules/)
- [Review new transactions](../../how-to/review-new-transactions/)
- [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
  section 3.6 and decision 48
