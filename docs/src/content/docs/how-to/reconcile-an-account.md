---
title: Reconcile an account
description: Match an account to its statement balance, see what Purser posts and records, and how a reconciliation stays honest when history changes afterwards.
---

Reconciling tells Purser "the statement says this account held X at the end
of this date". Purser compares X with its own balance for that date, posts one
adjustment for any difference, and records the claim. From then on, every read
of the account re-checks the claim, so a reconciled balance never goes stale
silently.

## Which accounts

Checking, savings, cash and credit-card accounts. These are the accounts whose
balance is the sum of their transactions.

Loan and property balances are valuations, and a brokerage balance includes
market value, so their statements are not reconciled this way. Record a
valuation for a loan or property instead, with `POST /accounts/{id}/valuations`
or the MCP tool `record_valuation` (see
[Recording a valuation](../../explanation/balances/#recording-a-valuation)).

Reconciling a credit card checks what the card owed on the statement date;
it does not change what you plan to pay. To make the upcoming card payment
the statement balance, record it on the payment instead (see
[Card statement balances](../../how-to/schedules-and-bills/#card-statement-balances)).

## Enter the balance as the statement shows it

Pass `statement_balance` exactly as it is printed, and Purser converts it:

| Account | The statement shows | You enter | Purser's ledger balance |
| --- | --- | --- | --- |
| Checking, savings, cash | 1250.00 in the account | `"1250.00"` | `1250.0000` |
| Credit card | 425.00 owed | `"425.00"` | `-425.0000` |
| Credit card | a 10.00 credit balance | `"-10.00"` | `10.0000` |

Purser keeps a card's balance negative when you owe money, so a statement's
positive amount owed is negated on the way in. Every amount in the answer is in
the ledger's sign, except `statement_balance`, which echoes what you entered.

## Pick the basis

Banks report one of two balances, so say which one the statement shows:

- `posted` (the default) counts cleared transactions only. This is the usual
  statement balance.
- `pending_inclusive` also counts pending transactions. It matches the
  "current balance" many banking sites show.

A pending hold dated before the statement date counts in `pending_inclusive`
and not in `posted`.

## Preview, then reconcile

`POST /accounts/{id}/reconcile` needs write scope. Start with a dry run. It
runs the same computation, conflicts included, and writes nothing:

```json
{
  "as_of": "2026-09-30",
  "statement_balance": "1250.00",
  "basis": "posted",
  "dry_run": true
}
```

```json
{
  "reconciliation_id": null,
  "account_id": "3f6c1e52-1b1e-4c55-9a7e-0b8f5d2a7c41",
  "as_of": "2026-09-30",
  "basis": "posted",
  "statement_balance": "1250.0000",
  "target_balance": "1250.0000",
  "ledger_balance": "1225.0000",
  "adjustment": "25.0000",
  "adjustment_tx_id": null,
  "category": "expenses.reconciliation_adjustments",
  "reconciled_through": "2026-09-30",
  "created": false,
  "dry_run": true
}
```

`ledger_balance` is Purser's balance on that date before any adjustment, and
`adjustment` is what it will post. If the adjustment is not what you expect,
look for a missing or duplicated transaction before you commit. Then send the
same body without `dry_run`. The answer is the same, with `created: true` and
the new ids.

`as_of` is the statement date, today (UTC) or earlier. A future date is
rejected.

## What Purser records

- **One adjustment, when needed.** A difference becomes one ordinary posted
  transaction dated on the statement date, in the category
  `expenses.reconciliation_adjustments`, with the payee "Balance
  reconciliation". It lands in the
  [unreviewed set](../../how-to/review-new-transactions/), so you see every
  adjustment, including one an agent made. If you later find what it was,
  recategorize it like any other transaction. The account balance does not
  change.
- **A clean reconcile too.** When the ledger already matches, nothing is
  posted, and the reconciliation is still recorded with an adjustment of 0.
- **The marker.** The account's `reconciled_through` moves to the statement
  date.
- **An audit row and an event.** Every reconciliation writes an audit row and
  emits [`account.reconciled`](../../reference/events-webhooks/).

## When history changes later

A transaction dated on or before a reconciled date can still arrive or change
after you reconcile: a late import, an edit or correction, or a pending hold
that disappears. Purser does not rewrite the reconciliation when that happens.
It re-checks the claim every time you read it:

- `GET /accounts/{id}` (and the accounts list) carries `reconciliation`, the
  latest reconciliation with `ledger_balance` (the balance on that date now),
  `difference` (ledger balance minus target) and `in_balance`.
- `GET /accounts/{id}/reconciliations` lists every reconciliation, newest
  first, each re-checked the same way.
- The agent tool `list_accounts` carries the same check for each reconciled
  account.

For example, you reconcile September 30 to 1250.00, and then a 10.00 deposit
dated September 29 imports late. The reconciliation now reads
`"in_balance": false`, `"difference": "10.0000"`.

To resolve it, find the change. If it was entered by mistake, correct it, and
the reconciliation reads `in_balance` again. If it is real, the adjustment
posted at the time was probably standing in for it. Reconcile the next
statement: its adjustment nets the difference out, and the older
reconciliation stays visible as out of balance.

## Retries and conflicts

Retrying is safe. The same date, balance and basis returns the recorded result
with `created: false` while the ledger still matches. Once the ledger has moved,
the retry fails with 409 instead of looking like a success. The response body
`detail` says why:

| `detail.code` | Meaning |
| --- | --- |
| `reconciliation_moved` | This reconciliation is recorded, but the balance on that date has changed since. `detail.difference` is the ledger balance minus the target. |
| `reconciliation_conflict` | A different balance or basis is already recorded for that date. Each account has at most one reconciliation per date. |
| `reconciliation_out_of_order` | The date is before the account's latest reconciliation. Reconcile that date or a later one. |

## For agents

The MCP tool `reconcile_account` takes the same inputs and needs write scope.
`account` is a UUID or a path:

```json
{
  "account": "liabilities.card",
  "as_of": "2026-09-30",
  "statement_balance": "425.00",
  "dry_run": true
}
```

Do a dry run first, read `adjustment` back to the user, and commit only when
they agree. Never flip a card statement's sign yourself. Purser does that.

A loan or property statement is not reconciled. Record its figure with the MCP
tool `record_valuation` (write scope), entered as printed, with
`convention` set to `statement`:

```json
{
  "account": "liabilities.mortgage",
  "as_of": "2026-09-30",
  "value": "250000.00",
  "convention": "statement"
}
```

Purser stores `-250000.0000`, the ledger sign of an amount owed, and reports it
as the loan's balance from then on. Without `convention`, `value` is read in
the ledger's sign, so a positive figure on a loan is refused rather than
recorded as an asset. Here too, never flip the sign yourself.

The design and its trade-offs are in
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 3.7 and decision 61.
