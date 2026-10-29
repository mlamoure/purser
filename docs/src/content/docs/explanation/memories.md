---
title: Memories
description: Short notes of financial nuance that Purser serves to your agent as context — and why they inform rather than act.
---

Some things about your finances are not in your ledger. That the monthly
transfer to a brokerage account is a child's 529 contribution. That one card
is always paid from one checking account. That a recurring payment is a
sublet, not rent.

An agent cannot derive any of that from transactions. **Memories** are where
you record it, and Purser serves them as context to whatever agent you connect.

## Rules act, review flags ask, memories inform

That division is the whole design.

A memory never changes what lands in your ledger. No code path parses the
text — there is no evaluator, no import hook, no scheduled job that reads one.
The absence is deliberate: free-form text that *acts* would undermine the
guarantee that only deterministic, user-declared boundaries decide anything.

If a nuance should actually **do** something, it belongs in a
[rule](../../how-to/rules/). Rules can cite the memory that motivated them, and
archiving that memory never disables the rule — rules are the executable layer
and must not change behavior because a note was tidied.

## Where they show up

- **`describe()`** carries a count plus your pinned few, so a fresh agent
  session is oriented without pulling everything.
- **`list_memories`** fetches more on demand, filtered by account or payee.
- **The review loop** attaches memories relevant to each row being reviewed.
- **The AI classifier**, when configured, gets them as prompt context — which
  is what lets it act on "the monthly transfer to Fidelity is a 529
  contribution" instead of guessing.

## Relevance is exact, never fuzzy

A memory reaches a transaction only when one of its links **is** that
transaction's account, its category, or its payee. Foreign-key equality — no
substring matching, no name similarity, no embeddings. A reviewer never
receives a memory about a different account.

Memories with no links are global and apply everywhere.

One consequence surprises people, so it is worth stating: a memory linked only
to a **category** cannot reach the classifier, because at categorization time
the category is the thing being decided. Link to an account or a payee if you
want the classifier to see it.

## Drafts are never served

When an agent records a memory it lands as a **draft**, unless you asked it to
remember something. Drafts appear in **Config → Memories** for you to approve,
and are not served anywhere — not in `describe()`, not in listings, not to the
reviewer, not to the classifier.

That is what stops an agent writing its own future context on its own say-so:
a memory the agent decided to keep waits for your approval. The same gate
applies to memories Purser writes itself, if you enable that.

There are caps for the same reason: 500 characters each, 100 active, 8 pinned,
and 25 outstanding drafts. The draft cap is the flood valve — nothing can fill
your inbox faster than you can empty it.

## Budgets, because context is not free

A hundred memories is roughly twelve thousand tokens, which would swamp any
agent's context. So serving is bounded at every width: `describe()` carries at
most 8 pinned rows and 1,000 characters, `list_memories` at most 25 rows and
2,500 characters, and the classifier prompt at most 5.

Every response reports exactly how much it truncated, so an agent always knows
what it did not see rather than assuming it saw everything.

## Privacy

Memory text is encrypted at rest alongside your payee names, descriptions and
memos.

Reading memories needs the `read` scope and writing them `write`. With the
purchase-privacy setting on, payee names inside a memory's text reach an agent
as references, like everywhere else (see
[Privacy and agents](../../explanation/privacy-and-agents/)). An agent creates a
memory as a draft unless you asked it to remember something: an active memory
is context for every future session.

## Archive, do not delete

Archiving is reversible and is the normal way to retire a memory. Hard
deletion is admin-only and recoverable only for as long as your audit
retention window keeps the record — your own prose is not derivable from
anything else in the system, so Purser makes it hard to lose.

Purser never edits or archives your text on its own. A memory whose accounts
were all closed is flagged **stale** and left exactly as you wrote it.

## See also

- [Write deterministic rules](../../how-to/rules/)
- [Review new transactions](../../how-to/review-new-transactions/)
- [`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
  section 3.6 and decision 48
