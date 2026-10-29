---
title: Manage transfers
description: How Purser links the two sides of a transfer deterministically, when it asks first, and how to convert, absorb, and unlink.
---

A transfer between your own accounts is not spending. In Purser it is **one
transaction with two real-account postings** — money leaving `assets.checking`
and arriving in `assets.savings` — not two unrelated rows. Getting the two bank
sides joined into that single transaction is what this page is about. A loan
payment whose bank sides differ by its interest is the one variation: it adds a
single interest/fee posting (see
[Confirm a split loan payment](../../how-to/transfers/#confirm-a-split-loan-payment)).

## Deterministic auto-linking

At import (and on the daily sweep), Purser looks for opposite-amount pairs
across accounts whose dates are at most 3 days apart. A pair is
**auto-linked** only when the amount-and-window match is joined by a
**signal**:

- a **name signal** — a transfer or payment-style keyword (`transfer`, `xfer`,
  `payment`, `autopay`, ...) or the counterparty account's own name in the
  text;
- a **declared pair** — an active `transfer_link` rule for the two accounts,
  which can also give that pair its own window (see
  [Declared pairs and their window](../../how-to/transfers/#declared-pairs-and-their-window));
- a **scheduled transfer** — a transfer schedule's instance covers the pair
  (see [Scheduled transfers](../../how-to/transfers/#scheduled-transfers) below).

When a signal is present the two one-sided rows merge into a single transfer
and the originals are voided with links back. This is deterministic by design
— no scoring, no guessing.

Every auto-merge is **logged, evented, badged, and reversible**: it writes an
audit row, emits a `transfer.auto_linked` event whose `signal` names what
linked it — `rule:<rule id>`, `name` or `schedule:<schedule id>`, the first
that applies — shows a badge in the register, and can be undone with unlink.
The auto path acts on **posted rows only**; pending pairs stay confirm-first
until they post. Turn it off entirely with the `PURSER_TRANSFER_AUTO_LINK`
setting (Config -> Settings, default on) — then every candidate waits for
you.

## Scheduled transfers

A [transfer schedule](../../how-to/schedules-and-bills/#recurring-transfers) — a
monthly savings sweep, a 529 contribution — already says which two accounts
move how much, and when. That is enough to link its bank rows, even when their
descriptors carry no keyword at all (`FID BKG SVC LLC MONEYLINE` on one side,
`MONEY LINE RECEIVED EFT` on the other). A pair is covered when an instance of
an **active** transfer schedule:

- names the same two accounts, in the same direction;
- has **exactly** the pair's amount — an estimated instance whose amount
  differs from the bank's is not a signal; link those by name, by rule, or by
  confirming;
- is dated within the 3-day window of **both** legs.

The bank legs of a transfer often post a day or more apart, and the nightly
job matches expectations before it links transfers, so the first leg is
usually matched to the instance by the time its counterpart arrives — proposed
when only the amount and date agree, matched outright when the description
agrees too. That leg stays linkable: a bank row matched to an expected
transfer still pairs with a row on that transfer's other account (never with
anything else), so the pair links whichever side posts first. Either way the
instance ends up **settled once, by the merged transfer**: a confirmed match
moves onto the merge, a proposal is replaced by an automatic match on it, and
an instance that was still open is matched to the merge at link time. The
merged transfer is then matched, so no other expectation can claim it. A bank
row matched to an ordinary bill is never pulled into a transfer this way.

When an estimated instance is off — the card statement says 8,347.00, not the
balance the schedule estimated — override the instance to the statement amount
([Rules and instances](../../how-to/schedules-and-bills/#rules-and-instances)). Both
of its legs move, and an instance at exactly the bank's amount is a signal
again, so the payment links when its bank rows arrive.

## Confirm-first candidates

An amount-and-window pair with **no** signal is a `transfer.candidate` event,
not an auto-link: it looks like a transfer but Purser will not merge it
silently. Confirm it yourself — `confirm_transfer` over MCP, or the register's
confirm control — to merge the pair, or leave it as two categorized rows. A
bank row already matched to an expected transfer is still listed with its
counterpart, and confirming the pair carries the match onto the transfer.

## From a confirm to a rule

A confirm merges that one pair and teaches Purser nothing hidden. What it does
instead is visible: it proposes a `transfer_link` rule for the two accounts,
scoped to the payee on the rows, as a suggestion in the
[suggestions inbox](../../how-to/suggestions/). The confirm's response carries its
`suggestion_id`. Confirm another pair between the same accounts with the same
payee, in either direction, and the same suggestion is refreshed, with each
pair you confirmed listed as its evidence.

Nothing links because of the suggestion until you decide it:

- **Accept** creates the rule as a draft and tells you how many past pairs it
  would link. Activating it asks you to confirm that count — the same
  confirmation as any [pair rule](../../how-to/rules/#activating-a-pair-rule). Once
  active, pairs between those accounts where one side carries the payee (or
  an alias of it) link on their own, past and future.
- **Edit and accept** narrows or widens the draft first: an exact amount, a
  date window of 0 to 14 days, or a start date.
- **Reject** is remembered: that pair and payee are never proposed again, or,
  with "never for this pair", that account pair with any payee.
- **Snooze** brings it back after 30 days.

Unlinking a pair you confirmed undoes that merge and nothing else. The
suggestion stays in the inbox for you to decide, and no rule exists until you
accept one, so the next pair from the same payee stays a confirm-first
candidate.

A split loan payment proposes no rule, since its unequal sides could never
link automatically, and neither does a pair whose rows carry no payee: one
confirm is not evidence about every transfer between two accounts.

## Declared pairs and their window

When two of your accounts transfer to each other regularly but the bank text
carries no signal, declare the pair with a `transfer_link`
[rule](../../how-to/rules/#declaring-a-transfer-pair) — or accept the one a confirm
proposed. For that pair the name signal is no longer needed, and the rule can
also set the pair's own date window with `window_days` (0 to 14 days; 3 when
unset). A card autopay whose
two sides post 4 days apart links once its pair rule has `window_days` of 4 or
more, while every pair you have not declared keeps the 3-day window.

A wider window never takes a row from a closer pair already in your ledger:
each pass links everything within 3 days first, exactly as it would without
the rule, and pairs the declared accounts farther apart only among the rows
left without a partner. Activating the rule asks you to confirm how many
historical pairs it will link, counted with its window.

## Confirm a split loan payment

A loan payment rarely shows the same amount on both sides. Checking reports the
whole payment leaving; the loan account reports only the principal it was paid
down by; the difference is interest or fees. Those two rows never auto-link
(every automatic path needs exactly opposite amounts), so confirm them yourself
and name the category the difference belongs to:

```bash
curl -sX POST https://purser.example.com/matching/confirm-transfer \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"outgoing_id": "CHECKING_ROW_ID", "incoming_id": "LOAN_ROW_ID",
       "remainder_category": "expenses.fees.interest"}'
```

Over MCP, call `confirm_transfer` with the same three arguments. With checking
at `-612.40` and the loan at `+540.15`, Purser records **one** transaction:

| Posting | Amount |
| --- | --- |
| `assets.checking` (the bank's cash leg, unchanged) | `-612.40` |
| `liabilities.carloan` (the loan's principal leg, unchanged) | `540.15` |
| `expenses.fees.interest` (the remainder) | `72.25` |

It sums to zero by construction, and it never invents a full-payment `612.40`
posting on the loan — each bank leg stays exactly what its bank reported. The
response, the audit row and the `transfer.confirmed` event carry the
`remainder` and its `remainder_category`.

The rules:

- The incoming (principal) amount must be positive and **smaller** than the
  outgoing (cash) amount. Equal legs are an ordinary transfer — confirm them
  without `remainder_category`. A principal larger than the cash, a zero, or a
  negative leg is refused with a 422 that names the problem.
- `remainder_category` must be an expense category, and not
  `expenses.uncategorized`.
- It needs **write** scope, and nothing automatic ever does it — no
  auto-link, absorb, rule or AI path produces a split.
- **Unlink** restores both bank rows exactly as they were, and the interest
  posting goes with the split. To change the category, unlink and confirm
  again.

In the register the row shows the other account as its `[account]` value, with
a **split** line under it giving the remainder amount and category. Over the
API the row carries `is_transfer: true`, `transfer_account_path`,
`category_path` (the remainder category) and `remainder_amount`; MCP
`search_transactions` rows carry `counterparty`, `category` and `remainder`.
Only the shapes Purser writes count as transfers: a row that touches two real
accounts and several categories is shown as what it is, not as a transfer.

## Convert a row into a transfer

In the flat view a transfer's "category" is the other account in Quicken-style
brackets, and the same sugar works for writes. Set a row's category to
`[assets.savings]` (via `update_transaction` or a PATCH) to convert it into a
transfer to that account. Depending on what already exists on the other side,
the conversion **absorbs** into a matching transfer, **merges** with a live
one-sided counterpart (including one already matched to an expected transfer
between the same two accounts — the match moves onto the merge), or converts
in place. Set a plain category back to undo
it, naming in `account` the side that keeps the row. A conversion must be the
**only** change in the call, and a transfer carrying linked bank rows must be
unlinked first.

## Change a transfer's amount

An expected or pending transfer takes a new amount in place: send `amount`
with `account` naming the side it is read from, and both legs move (a schedule
instance defaults to its schedule's account). The direction never changes — an
amount whose sign would reverse the transfer is refused. A transfer carrying
linked bank rows, a split loan payment and a posted transfer are not edited
this way: unlink them so their bank rows come back as separate transactions
(convert a posted transfer without bank rows back to a category instead).

## Absorb and unlink

**Absorb** handles the late-arriving side: when an imported row matches an
existing transfer's unbacked side and carries a name signal (or the pair is
declared, within the rule's window), it is voided into that transfer instead
of duplicating it (a `transfer.absorbed` event). **Unlink** undoes a link —
the merged transfer splits back into its one-sided originals (with their
content hashes moved onto the resurrected rows) and a `transfer.unlinked`
event fires. If the transfer settled an expectation, such as a scheduled
transfer's instance, that settlement goes with it: the instance is due again
in its slot and the next matching run decides what settles it — it is never
brought back as a posted copy. A pair you explicitly unlinked is suppressed
from the **auto** paths only; an explicit confirm or convert can always relink
it.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
sections 5 (matching and transfer tiers) and 3.3 (bracket transfers and split
loan payments). See
[Events and webhooks](../../reference/events-webhooks/) for the transfer events.
