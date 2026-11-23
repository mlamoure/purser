---
title: Start an account from the bank's balance
description: Give a bank-fed account a starting balance from the balance its bank reports, so it matches the bank from the first sync, and see how Purser keeps that entry in step when older history arrives.
---

A bank feed delivers about three months of transactions and no balance from
before them. A newly mapped checking account that the bank says holds
5,000.00 therefore reads as the sum of those three months, perhaps 847.66,
and net worth is off by the difference on every account.

A **starting balance** closes that gap with one entry: what the account held
before the first transaction Purser knows. Purser derives it from the balance
the bank reported in the latest sync. Nobody passes a date, a sign or a
figure.

## When it is set

- **Mapping a bank account.** The map form (the setup wizard and **Settings ->
  Connections**) has **Start from the bank's balance**, ticked for a new
  account and unticked when you map onto an existing one. Leave it ticked and
  the account matches the bank as soon as it is mapped.
- **Later, for an account that has none.** An agent calls
  `set_starting_balance(account)`; over REST it is
  `POST /accounts/{id}/starting-balance` (write scope). Both take a `dry_run`
  that answers the same without writing, and both are safe to repeat: an
  account that already has one is answered with it.

It applies to checking, savings, cash and credit-card accounts, whose balance
is the sum of their transactions. A brokerage account's balance includes its
holdings, and a loan or an asset is valued, so they need none.

## What Purser posts

One transaction, payee **Starting balance**, for the difference between the
bank's balance and the account's ledger:

| | Example |
| --- | --- |
| Bank's reported balance | `5000.00` |
| Transactions the feed delivered | `847.66` |
| Starting balance | `4152.34` |

- **Dated the day before the account's earliest transaction**, so every
  balance from the first feed row onward reads as the bank would have shown
  it, and a balance chart does not jump on the day you set Purser up.
- **Against the system account `equity.opening_balances`.** It is neither
  income nor spending, so no report counts it. The register labels the row
  and offers no category for it.
- **In the feed's own sign.** A card the bank reports at `-400.00` (400.00
  owed) with 75.00 of charges in the feed starts at `-325.00`.
- **Counting every recorded transaction**, including rows a bank dates a day
  ahead, so there is nothing to wait for.
- **Shown for review.** Purser made the entry, so it joins the review set
  (flag `starting_balance`) for a person to see.

Afterwards `account_balance` reports `bank_snapshot.comparison.difference` of
zero.

## Older history arrives later

The starting balance stands for everything before the account's first feed
transaction. When older transactions arrive, from a
[QIF or CSV import](../import-qif-csv/), a feed backfill or a row you enter
with an earlier date, Purser lowers the starting balance by exactly their sum
and moves it to the day before the new earliest row. Continuing the example,
importing a file with `-40.00` and `900.00` from two years ago:

| | Before | After |
| --- | --- | --- |
| Starting balance | `4152.34` | `3292.34` |
| Older rows | none | `860.00` |
| Balance today | `5000.00` | `5000.00` |

No balance on or after the first feed transaction changes, so you can import
history at any time and in any order. Purser does this after every import and
sync; the change is audited (`account.starting_balance`, reason
`older_history`) and emits `account.starting_balance_set`. If the file reaches
back to the day the account was opened, the starting balance ends at zero.

A row that arrives *inside* the feed's window is different: it is not older
history, so the starting balance stays and the comparison with the bank
reports the difference, as for any account.

## Pending transactions and the bank's basis

Some banks report a balance that includes pending transactions and some leave
them out. Purser learns which from an account's sync history
([the drift check](../simplefin/)). A new account has no history, so its
starting balance is first derived with pendings included; if the sync history
later shows that the bank leaves them out, Purser re-derives the entry on that
basis (reason `basis`). A basis you pin is used straight away. Purser stops
following the basis as soon as you reconcile the account to a statement.

## It is not a reconciliation

A starting balance records no statement and does not move
`reconciled_through`. To state "the statement said X on this date", use
[Reconcile an account](../reconcile-an-account/); that stays an explicit
entry you make. The two work together: reconcile to a statement whenever you
like, and the starting balance keeps absorbing older history without moving
the balance your reconciliation recorded.

Purser refuses a starting balance (HTTP 422, `detail.code`) rather than guess:

| Code | Meaning |
| --- | --- |
| `account_type` | Not a checking, savings, cash or credit-card account. |
| `not_connected`, `connection_disabled`, `account_closed` | No bank feeds this account now. |
| `no_snapshot`, `account_missing`, `ambiguous_account`, `no_bank_balance` | The latest sync has no usable balance for it. |
| `currency_unknown`, `currency_mismatch` | The bank's balance is in another currency. |
| `stale_balance` | The bank's balance is older than the stale-feed threshold: sync the connection first. |
| `reconciled` | The account is reconciled to a statement and differs from the bank: reconcile a later statement instead. |

## Take it out

The row is Purser's own, so it cannot be edited or recategorized. To remove
it, an agent calls `set_starting_balance(account, remove=true)`; over REST it
is `DELETE /accounts/{id}/starting-balance`. The entry is reversed to zero and
Purser stops keeping it.

```bash
curl -s -X POST "$PURSER_URL/accounts/$ACCOUNT_ID/starting-balance" \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'
```

```json
{
  "account_id": "8c0f0a52-6c1e-4a57-9d0c-0e1f5a3b7c11",
  "status": "set",
  "amount": "4152.3400",
  "entry_date": "2026-08-24",
  "basis": "pending_inclusive",
  "bank_balance": "5000.0000",
  "bank_balance_date": "2026-11-21",
  "transaction_id": null,
  "dry_run": true
}
```
