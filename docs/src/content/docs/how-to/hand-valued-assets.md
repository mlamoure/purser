---
title: Track a house or other asset
description: Keep the value of something you own and value by hand, such as a house or a car, as a ledger of changes, set its value on a date, and undo a wrong entry.
---

A house, a car, land or jewelry has no bank feed that reports its value. In
Purser such a thing is an account of type `property` (the app calls it an
asset), and its value is a ledger like any other account: the sum of its
entries.

- **A change in value is an entry.** "The house went up 25,000.00 in June" is
  one posted transaction between the asset and the system account
  `equity.revaluation`. That account is not a category, so the change moves
  net worth and never shows up as spending or income.
- **A backdated change works like a backdated transaction.** It moves every
  balance after it.
- **"Worth X on D" is a calculator.** Reconcile the asset to a figure and
  Purser posts the difference as a change.
- **A wrong change is reversed,** never edited, so the history keeps both.

## Create the asset

In the app, open **Accounts** and choose **Add asset**: a name, what it is
(house, land, car, boat, RV, jewelry, art & collectibles, equipment or other),
the purchase date and price, and an optional description such as an address
or a VIN. The description is stored encrypted. The kind is a label and an
icon only; it changes nothing about how the asset is valued, and it can be
changed later.

Over the API:

```bash
curl -s -X POST "$PURSER_URL/accounts" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "House", "type": "property", "path": "assets.house",
       "asset_kind": "house", "acquired_on": "2020-06-15"}'
```

`PATCH /accounts/{id}` changes `asset_kind`, `acquired_on` and
`asset_description` later (an explicit null clears the date or the
description). From an agent, `update_account` takes `asset_kind`.

## Money put in

What you paid for the asset is not a gain, so it is recorded as **money put
in**: a value change with `"kind": "capital"`, an entry against the system
account `equity.capital` instead of `equity.revaluation`. The app's
**Add asset** form records the purchase price this way.

```bash
curl -s -X POST "$PURSER_URL/accounts/$HOUSE_ID/value-changes" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d '{"as_of": "2020-06-15", "amount": "450000.00", "kind": "capital", "note": "Purchase price"}'
```

Use it for money no account in Purser paid: the purchase before your
history starts, or an improvement paid in cash. When a tracked account paid
(a new roof paid from checking), turn that payment into a transfer to the
asset instead (in the register, choose the asset as the category, or
`update_transaction` with `category="[assets.house]"`). Either way it raises
the asset's value without counting as a gain.

## Record a change in value

```bash
curl -s -X POST "$PURSER_URL/accounts/$HOUSE_ID/value-changes" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d '{"as_of": "2026-06-30", "amount": "25000.00", "note": "Appraisal"}'
```

`amount` is signed: positive when the asset gained value, negative when it
lost value. `as_of` is today (in the household time zone) or earlier. The
answer carries the entry's `transaction_id` and the asset's `balance` now.

`reference` makes a retry safe. Send the same reference again on the same
asset and Purser posts nothing and answers `200` with the first change
(`created: false`). A reference is stored in plaintext, so it takes letters,
digits, `.`, `_`, `:` and `-` only (for example `valuation-2026-06`); put
words in `note`, which is stored encrypted.

From an agent, the MCP tool `record_value_change` does the same (write scope).

## Set the value on a date

When you have a figure rather than a change (an appraisal, an estimate),
reconcile the asset to it:

```bash
curl -s -X POST "$PURSER_URL/accounts/$HOUSE_ID/reconcile" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d '{"as_of": "2026-11-01", "statement_balance": "612500.00", "note": "Estimate midpoint"}'
```

Purser compares the figure with the asset's value on that date and posts the
difference against `equity.revaluation` (`category` in the answer), dated
`as_of`. It follows the [reconciliation rules](../reconcile-an-account/):

- **One value per date.** Sending the same figure for a recorded date again
  answers `created: false` and posts nothing, which makes a scheduled job safe
  to retry. A different figure for that date is `409 reconciliation_conflict`.
- **Only forward.** A date before the asset's latest recorded value is
  `409 reconciliation_out_of_order`. To change history, record a backdated
  change instead.
- **Never stale silently.** A later backdated change, or a reversal, moves the
  value on a recorded date. That value then reads out of balance, with the
  difference, on `GET /accounts/{id}/reconciliations` and in `list_accounts`.

From an agent, use `reconcile_account` with the value as `statement_balance`.

## Undo a wrong change

```bash
curl -s -X POST "$PURSER_URL/value-changes/$TRANSACTION_ID/reverse" \
  -H "Authorization: Bearer $PURSER_TOKEN"
```

This works for a change recorded directly and for one a reconcile posted.
Purser corrects it through a reversal pair, so the original stays in the
history next to the reversal. The answer's `amount` is what was taken back.
Editing the row with `PATCH /transactions/{id}` is refused. From an agent, use
`reverse_value_change`.

## Get reminded to update a value

Give an asset `revalue_after_days` (on creation, `PATCH /accounts/{id}`, the
app's **Edit details**, or `update_account(revalue_after_days=...)` from an
agent; null, or `0` from an agent, turns it off). When the asset's value was
last stated longer ago than that, Purser:

- marks it `stale: true` on the account (`asset.last_valued` says when),
- emits `asset.stale` once, from the daily check at 07:20 UTC,
- and lists it in `whats_new` under `stale_assets` until a value is recorded.

Any live value entry counts as stating the value, and so does a recorded
value on a date even when it matched and posted nothing. A monthly estimate
that did not move still refreshes the asset. A reversed entry does not count.

## Equity and the loans an asset secures

Point a loan at the asset that secures it (a mortgage at its house, a car
loan at its car) and the asset reports its **equity**: its value plus the
loans' balances, which are negative while owed.

```bash
curl -s -X PATCH "$PURSER_URL/accounts/$MORTGAGE_ID" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d "{\"secured_by_id\": \"$HOUSE_ID\"}"
```

The asset then carries `equity` and `secured_loan_ids` on `GET /accounts`
(with balances) and `GET /accounts/{id}`; the loan carries `secured_by_id`.
An explicit null unlinks it. In the app, the asset's page lists its loans
with **Link a loan** and **Unlink**. From an agent, `update_account` takes
`secured_by` (the asset's path, or `""` to clear it), and `list_accounts`
shows `asset_kind`, `equity`, `secured_loans` and `secured_by`.

## Money put in and gain

Every asset reports two figures that add up to its value: **money put in**
(the purchase and every transfer in or out) and **gain** (the sum of its
changes in value; negative is a loss). They are on the account
(`asset.money_put_in`, `asset.gain`), in `list_accounts`, and on the asset's
page. They are ledger sums, not a tax calculation.

## Sell or dispose of an asset

1. Record the sale's money as transfers out of the asset, dated on or before
   the sale: the bank's deposit of the proceeds (in that account's register,
   choose the asset as the category) and any loan payoff made at closing.
2. Then dispose of it, in the app with **Sell or dispose…** on the asset's
   page, or:

```bash
curl -s -X POST "$PURSER_URL/accounts/$HOUSE_ID/dispose" \
  -H "Authorization: Bearer $PURSER_TOKEN" -H "Content-Type: application/json" \
  -d '{"as_of": "2027-06-30", "note": "Sold", "dry_run": true}'
```

Whatever is left on the asset on that date is the rest of its change in value
(selling costs included): a gain or a loss, or, when no proceeds were recorded,
its whole value (a write-off, such as a car that was totalled). Purser posts it
against `equity.revaluation` on the sale date and closes the account at zero,
so net worth drops on the sale date even when you record it weeks later, and
nothing lands in a spending category. `value_before`, `closing_change` and the
total `gain` come back; run it with `dry_run` first. A sale dated before the
asset's latest entry is refused. From an agent, use `dispose_asset`.

Closing an asset with **Close…** (or `POST /accounts/{id}/close`) does the same,
dated today.

## Where the value shows

- The asset's balance on every surface is its postings sum
  (`balance_method: "postings"`), the same as a checking account's.
- Net worth adds it like any other account.
- In the app, the asset's page shows its kind, equity, **Record value**
  (a value on a date) and **Change value** (a change, or money put in), and
  every entry in the register below with **Undo**.
- In the register, its entries carry `value_change: true` (and
  `money_put_in: true` for money put in). They never need review: reverse
  one when the figure is wrong.

Each change emits an `asset.value_changed` event (see
[Events and webhooks](../../reference/events-webhooks/)).

## An asset with a bank feed

A few feeds report the value of an account mapped as `property`. After each
sync, when the reported balance differs from the asset's balance on that day,
Purser posts a value change for the difference (origin `import`, note
"Balance the bank reported"). Such an asset is never part of the
balance-drift check.

## Loans are different

A loan's balance is still its latest valuation (`record_valuation`), because a
mortgage feed reports a balance and nothing else. See
[Recording a valuation](../../explanation/balances/#recording-a-valuation).
