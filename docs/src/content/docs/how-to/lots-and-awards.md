---
title: Track tax lots and equity awards
description: Load a broker's lot list and award schedule into a hand-tracked position, record lot-aware sales and vests, and correct mistakes, through the REST API or MCP.
---

A position you [track by hand](../../how-to/manual-holdings/) can also track
its **tax lots**: when each block of shares was acquired, how many remain, and
what they cost. An account can also hold **equity awards** (grants with a vest
schedule). Purser stores and checks this data and serves it back. Reading it
off a broker's site is your job or your agent's: Purser has no broker logins,
no scraping and no scheduling of its own.

Lots and awards are a sub-ledger:

- The share count is still the history of the position's events, so prices,
  balances and net worth work exactly as before.
- A position that tracks lots reports its lots' basis as its `cost_basis`.
  The basis is `null` while any share sits in a lot with no basis or outside
  every lot. It is never an average.
- Unvested award units are never counted in net worth, balances or holdings.

The design and every rule are in
[DESIGN.md section 8](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
(decision 107).

## Load a broker's lot list

A **capture** is one observation of the broker's lots as of a day. Send every
lot row the broker shows for a symbol, with the shares **remaining** in it:

```bash
curl -X POST https://purser.example.com/accounts/<account-id>/lot-captures \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "as_of": "2026-11-14",
    "captured_at": "2026-11-14T15:02:00Z",
    "completeness": "complete",
    "source_note": "broker holdings page",
    "positions": [{
      "symbol": "ACME",
      "lots": [
        {"external_ref": "L-1001", "acquired_on": "2024-02-05", "quantity": "12.5",
         "basis_per_share": "101.250000", "plan": "Monthly purchase", "acquisition": "plan_purchase"},
        {"external_ref": "L-1002", "acquired_on": "2025-08-05", "quantity": "3",
         "plan": "Matching award", "acquisition": "rsu_vest"}
      ]
    }]
  }'
```

- **A dry run is the default.** The answer is the plan: changes such as
  `new_lot`, `recount`, `fill_basis` and `assign_ref`, any conflicts, and a
  `plan_hash`. Nothing is written.
- **To apply**, send the same body again with `"dry_run": false` and
  `"expect_hash": "<plan_hash>"`. If anything changed in between, the answer
  is `409` with `preview_stale` and a fresh `plan_hash`. Review the plan
  again before you commit.
- **Retries are safe.** The exact same request again (the same `captured_at`,
  `as_of`, note and rows) writes nothing and returns the first capture
  (`retry: true`). The same holdings read again later, with a new
  `captured_at`, records a new capture for freshness (`repeat_of`) and
  changes nothing else.
- **Identity.** A row matches a lot by `external_ref`, the broker's lot
  reference, when it shows one. Otherwise it matches by acquisition date,
  plan and `ordinal`. Rows of one date and plan without a reference need
  distinct ordinals, kept in a stable order between captures. Several lots
  from the same vest stay separate.
- **Basis.** Give `basis_per_share`, `cost_basis` (the total for the
  remaining shares), or both if they agree. A total you give is kept exactly
  as given. Leave both out when the basis is unknown: it stays unknown and is
  never stored as 0. A row with no shares left carries no basis.
- **`complete` or `partial`.** A `complete` capture lists the broker's whole
  list for each symbol it names. Purser then recounts the position **up** to
  the broker's total, because new lots explain the extra shares. A
  `partial` capture changes only the rows it lists and never changes the
  share count. Symbols a capture does not name are left alone.

Over MCP the same contract is `upsert_lots(account, as_of, captured_at,
completeness, positions, dry_run=true)`. Then commit with `dry_run=false,
expect_hash=...`. `captured_at` is required: a commit must repeat the request
exactly.

### What becomes a conflict

Purser never infers a sale, a deletion or an edit from a capture. When a
capture shows something the book cannot explain, it is a conflict, and
nothing is applied for that symbol:

| Conflict | Meaning | Resolve it by |
| --- | --- | --- |
| `quantity_decrease` | A lot has fewer shares than Purser holds for it | Recording the sale (or transfer out) that drew on it, naming the lot, then capturing again |
| `quantity_increase` | A lot has more shares | Reversing the wrong disposal, or voiding the lot and capturing again |
| `missing_from_capture` | A complete capture leaves out a lot that still holds shares | Recording what took it, or voiding it if it was wrong |
| `unexplained_decrease` | A complete total is below Purser's count, with every lot accounted for | Recording the disposal first |
| `basis_differs` | The broker's basis differs | Capturing again with `basis_policy: "replace"` |
| `basis_locked` | The basis differs, and a disposal already drew on the lot | Reversing the disposal, correcting the basis, recording the disposal again |
| `insufficient_unallocated` | A partial capture's new lots need more shares than sit outside lots | Using a complete capture, or recording the shares first |
| `ambiguous_identity` / `identity_mismatch` | Rows and lots cannot be told apart, or a reference names a lot of another date or plan | Adding references or ordinals |
| `stale_capture` | The capture is older than one already applied: an earlier `as_of`, or the same day read at an earlier `captured_at` | Nothing: it is compared, never applied |

`skip_conflicts: true` applies the symbols that have no conflicts. It skips
the others whole and still records the capture as evidence.

## Sell or move shares from lots

On a day a position's lots can be drawn on, a `sell` or `transfer_out` must
name the lots it takes. Without them it is refused with
`allocation_required`.

```bash
curl -X POST https://purser.example.com/positions/<position-id>/events \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind": "sell", "occurred_on": "2026-11-14", "quantity": "5", "price": "150",
       "lots": [{"external_ref": "L-1001", "quantity": "5"}]}'
```

- **Specific identification:** `lots: [{lot_id | external_ref, quantity}]`.
  `{"unallocated": true, "quantity": ...}` takes shares that sit outside every
  lot. Use the broker's own lot choice when it shows one.
- **A method:** `lot_method` is one of:
  - `fifo`: oldest acquisition first.
  - `lifo`: newest first.
  - `hifo`: highest basis per share first. It is refused while a candidate
    lot has no basis.

  A method never takes unallocated shares.
- **Cash.** A sale's cash works exactly as on any hand-tracked position: the
  double-count check, `transaction_id` links and `settle_on` all apply.
- **What is recorded.** Each disposition records its basis, its share of the
  net proceeds, the realized gain when both are known, and the term. A
  disposal is `long` only after the one-year anniversary of the acquisition.
- **Basis is conserved exactly.** The disposal that empties a lot takes the
  remaining basis. A sale's proceeds are split across its lots exactly, and
  no lot gets a share of the other sign.
- **`transfer_out`** records shares that left with no price and no cash, such
  as a move to another broker. It is never a sale.

Over MCP: `record_sale(..., lots=[{lot, quantity}])` or
`record_sale(..., lot_method="fifo")`, and `record_transfer_out`. A `buy` on
a lot-tracked position opens its own lot. Pass `"unallocated": true` to keep
the shares outside every lot.

## Load awards and record vests

An award capture lists each grant (`grant_ref`, `symbol`, `granted_on`,
`granted_quantity`, `cancelled_quantity`, the broker's `vested_quantity`) and
the vests still to come (`schedule: [{vest_date, quantity}]`). Send it to
`POST /accounts/{id}/award-captures`, or over MCP to `upsert_awards`. It
follows the same dry-run, `expect_hash`, retry and conflict contract as a lot
capture.

Purser applies these changes:

- new grants and new vests
- a re-estimated scheduled quantity
- more cancelled units
- a new reported vested count

These are conflicts, never applied:

- a changed grant size
- fewer cancelled units
- a scheduled future vest that a complete capture no longer lists. Purser
  never forfeits it by itself; forfeit it explicitly with
  `PATCH /award-vests/{id}` (MCP `update_award_vest`).

Purser works out each award's units from all the evidence:

- **Vested:** the broker's reported vested count, plus the vests you
  recorded after its date. Purser keeps that count when a later capture
  leaves it out. Vests you recorded on or before its date are already in it,
  so they are never counted twice.
- **Cancelled:** the larger of the broker's cancelled count and the units of
  vests you marked forfeited. Both describe units that will never vest, so a
  forfeiture the broker also reports as cancelled is counted once.
- **Unvested:** granted minus cancelled minus vested. It is split into
  **scheduled** units (dated vests still to come) and **unscheduled** units
  (no vest date yet).
- **Unknown:** if the broker never reported a vested count and the recorded
  vests and schedule don't account for every unit, Purser reports only the
  scheduled units and marks the total as not known (`unvested_known: false`,
  `complete: false`).
- **Limits:** a capture, a vest, reinstating a vest, or correcting granted or
  cancelled units is refused if vested, scheduled and cancelled units would
  add up to more than the grant.

When a vest happens, record it:

```bash
curl -X POST https://purser.example.com/awards/<award-id>/vests \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"vest_date": "2026-11-05", "gross_quantity": "10", "withheld_quantity": "3.5", "fmv": "60"}'
```

- **By default** a `vest` event adds the net shares (gross minus withheld)
  and opens their lot, with the fair market value as its basis.
- **All units withheld** (`withheld_quantity` equal to `gross_quantity`): the
  vest is recorded with no shares and no lot.
- **If you already captured the broker's lot list,** the vest's lot is
  already there. Pass `"lot": "<lot id or reference>"` to **link** it
  instead: no shares are added. The lot must meet all of these:
  - on the award's account and symbol
  - acquired on the vest date
  - holding the net shares, or fewer if some were sold before the capture
  - with the fair market value as its basis
- **Duplicates are refused.** Purser refuses the vest as a
  `possible_duplicate` when an unlinked lot acquired that day exists and
  neither `lot` nor `force_new_lot` was given. A vest is never counted twice.
- **Retries are safe.** The same vest again with the same figures is a no-op
  (`already_recorded`). Different figures are a `409 vest_conflict`.

Unvested units appear in these places, always outside net worth:

- `GET /accounts/{id}/awards` and MCP `list_awards`: the units per symbol,
  their value at the latest close, and the next vest.
- MCP `list_holdings`: an `unvested` block per account (the three largest
  symbols; `unvested_more` counts the rest).

`list_awards` answers a page at a time: the awards, or with `award` one
award's vest schedule, continue with `next_cursor`. The per-symbol summary
comes on the first page.

## Read it back

- **Lots:** `GET /positions/{id}/lots` (MCP `list_lots`) returns the lots in
  FIFO order, a page at a time. Its summary gives:
  - the shares in lots, and the shares outside every lot (unallocated)
  - the shares in lots with no basis
  - the lots' total basis
  - `capture`: when the broker's lots were last captured

  The price's own date, `price_as_of` on the position, is a different kind
  of freshness.
- **One lot:** `GET /lots/{id}` returns a lot and every disposition that
  drew on it. A disposition shows `live: false` once its event is reversed.
- **Realized gains:** `GET /accounts/{id}/realized-gains?year=2026` (MCP
  `realized_gains`) gives totals by term. Shares without a basis or proceeds
  are counted, never valued as 0. Purser computes no tax.
- **Captures:** `GET /accounts/{id}/captures` lists the account's captures,
  newest first.

## Correct a mistake

Every correction is audited. Events are corrected by reversal; lot-basis
corrections update the lot and retain the previous and new basis in the audit log:

- **A lot opened by an event** (a buy or a vest) goes when you reverse that
  event: `POST /position-events/{id}/reverse`, MCP `reverse_position_event`.
  The lot shows as `reversed`, and its reference is free, so you can record
  the corrected buy or vest with the same reference. The reversal is refused
  while a disposal still draws on the lot: reverse the disposal first.
- **A lot adopted from a capture** is voided with `POST /lots/{id}/void` (MCP
  `void_lot`). Then capture again with a new `captured_at` to record the
  right lot. The void is refused while a disposal draws on the lot or a vest
  is linked to it.
- **A wrong basis** is corrected with `PATCH /lots/{id}` (MCP
  `correct_lot_basis`), only while no disposal has drawn on the lot.
  Otherwise reverse the disposals, correct the basis, and record them again.
- **A vest linked to the wrong lot**, or one recorded with every unit
  withheld, goes back to scheduled with `PATCH /award-vests/{id}`
  (`"status": "scheduled"`).

## Privacy

Lot references and grant ids are account identifiers. They are encrypted at
rest. Agents never see them unless a call asks with `include:
["external_ids"]`, and REST omits them for non-admin API tokens. You can
always pass a reference to name a lot or an award. Plan labels follow the
purchase-privacy setting. A lot's basis (its per-share figure too), proceeds
and gains follow the amount-privacy setting. Realized-gain totals are
aggregates.

## Not covered

These are out of scope:

- tax forms and tax computation
- ESPP qualifying flags
- splits and other corporate actions
- importing a CSV lot export
- vest events in the forecast

For `record_vest` and `POST /awards/{id}/vests`, `withheld_quantity` is
required. Supply explicit zero only when the evidence confirms nothing was
withheld. Sold or withdrawn shares do not establish tax withholding. If the
historical withholding is unknown, keep the owned lot and award baseline and
leave that vest event unrecorded until evidence is available.

Importing lots does not correct cash. For a manual brokerage account, use
[cash reconciliation](../reconcile-an-account/) with `component: "cash"`,
excluding both securities and unvested awards from the cash target.
