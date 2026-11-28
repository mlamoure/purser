---
title: Enter holdings in Purser
description: Enter what a brokerage account holds when SimpleFIN sends its cash but no holdings, keep those positions priced with Market data, and see how they reach balances and net worth.
---

Some brokerages send an account's cash through SimpleFIN but no holdings. An
employee stock plan is the usual case: the shares sit at the broker, the feed
shows only the cash that leaves. Without holdings, the account reports its cash
alone and net worth leaves the shares out.

Purser can hold such an account's holdings itself. You (or your agent)
enter what the account holds, Market data prices it every day, and the value
reaches the account's balance and net worth exactly like a feed snapshot would.

An account whose holdings come from the feed needs none of this: its buy and
sell rows are marked as securities trades instead, and its cash is kept in step
with the broker's balance after each sync (see
[Mark brokerage buys and sells](../../how-to/brokerage-trades/)). On an account whose holdings
are entered in Purser, record the trade and link its bank row; marking a row there is
refused.

## Switch the account to manual holdings

An account's holdings come from one source at a time: the feed (the default) or
positions entered in Purser. An admin switches a brokerage account, once:

```bash
curl -X PATCH https://purser.example.com/accounts/<id> \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"holdings_source": "manual"}'
```

In the admin UI the switch is the **Holdings** selector on the account page
(admins only).

- The feed keeps importing the account's **cash** transactions. It stops
  recording **holdings** for it: those rows are counted as
  `holdings_skipped_manual` in the sync result, and they stay in the archived
  fetch.
- Feed snapshots recorded before the switch stay as history. The first
  snapshot of holdings entered in Purser is dated the day after the last feed one, so no date
  mixes the two sources.
- Switching back to `feed` needs the account to be mapped to a feed account
  (a 422 otherwise) and changes no snapshot: the holdings entered in Purser stay
  in effect, so the account keeps its value, until the first sync after the
  switch ends the manual period. From that sync's date the account
  holds what the feed reports, and a position the feed does not report ends
  there (all of them, for a feed that sends only cash). The sync result counts
  what it ended (`manual_period_tombstones`), and the audit log records it as
  `account.manual_period_end`. The history entered in Purser before stays, and so
  do your positions and their events, read-only, until the account is
  switched to manual again. Unmapping or closing the account before that sync
  ends the period the same day; remapping it onto another feed account does
  not (that mapping's first sync ends it). If no sync of the account arrives
  for the price staleness threshold (7 days by default), the daily price job
  reports it once (`holding.period_open`, and a `whats_new` line): the
  holdings entered in Purser are still counted, unpriced by anything new, so check
  the connection or the account's mapping.

Only brokerage accounts take a holdings source.

## Record what the account holds

"I hold 412.5 shares of ACME as of 2026-10-14" is one call. From an agent:

```text
record_position(account="assets.plan", symbol="ACME",
                quantity="412.5", as_of="2026-10-14",
                source_note="broker holdings page, 2026-10-14")
```

Over REST, open the position with its opening quantity:

```bash
curl -X POST https://purser.example.com/accounts/<id>/positions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"symbol": "ACME", "quantity": "412.5", "as_of": "2026-10-14"}'
```

In the admin UI, the account page gains a **Positions** tab with
**+ position** and a **Set quantity** editor on every row.

- `quantity` is a decimal string, up to 6 places. `cost_basis` (optional) is the
  TOTAL basis of that quantity; with it, positions report an unrealized gain.
  Left out, a re-count of the same number of shares keeps the basis, a
  changed count makes it unknown, and zero shares have a basis of zero. The
  **Set quantity** editor sends a basis only when you type one; the field
  shows the current basis as a hint.
- `as_of` is today or earlier. A backdated quantity re-values history from that
  date, and net worth heals by itself.
- Writes need **write** scope. A position has no draft state: a record is
  final, and a correction is a new event.

## Buys, sells and corrections

A position's history is a list of events:

| Event | Means |
| --- | --- |
| `set_quantity` | The account holds exactly this many shares on that date. |
| `buy` | It gained this many shares; with a `price`, the cost basis grows by quantity × price + fees. With `funded_from: "cash"` its cost also leaves the account's cash (below). |
| `sell` | It lost this many shares; the basis drops at average cost. Its proceeds land on the account's cash unless `proceeds_to` is `"none"` (below). |
| `split` | A stock split: every share becomes `split_numerator` / `split_denominator` shares on its effective date, with the same total basis ([below](#record-a-stock-split)). |

```bash
curl -X POST https://purser.example.com/positions/<position id>/events \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "sell", "occurred_on": "2026-10-01", "quantity": "20", "price": "101.25"}'
```

Every write is checked against the whole history: a backdated sell that would
leave the position below zero on any later date is refused (422), naming that
date. An event dated in the future is refused too; record it once it has
happened.

Events are never edited. To correct one, reverse it
(`POST /position-events/<id>/reverse`) and record the right one with
`"replaces": "<id>"` (same date). The correction then takes the reversed
event's place in that day. Without `replaces`, a new event goes after every
event already recorded that day: re-recording an opening count after a buy on
the same day would apply the count last and drop the buy. A position
that holds nothing can be closed (`PATCH /positions/<id>` with
`{"closed": true}`, or **Close** on its row in the Positions tab); a closed
position is read-only history. A position that holds nothing is not fetched,
flagged stale or listed in the price status even before you close it. A new position
in the same symbol starts after the closed one's last date (an earlier date
is refused, 409), so the same shares are never counted twice.

### Correct a closed position

A position's last sale usually leaves it at zero, and once it is closed its
events can no longer be reversed. To correct it:

1. Reopen it: `PATCH /positions/<id>` with `{"closed": false}`, or
   `reopen_position(position_id)` from an agent (`record_sale` answers with the
   `position_id`, and the "position is closed" refusal names it).
2. Reverse the wrong event (`POST /position-events/<id>/reverse`, or
   `reverse_position_event(event_id)` from an agent); a sale's cash is undone
   with it.
3. Record the right event.
4. Close it again (`{"closed": true}`).

The reopen is refused (409) while another position in the same symbol is open
on the account, or when a later position in the symbol exists: its history
follows this one's, so correct that one instead.

## Record a stock split

When a stock splits, record a `split` event with its ratio, as new shares for
old, dated on the effective date: the first day the shares trade split. A
2-for-1 forward split is `2` for `1`; a 1-for-10 reverse split is `1` for `10`.

```bash
curl -X POST https://purser.example.com/positions/<position id>/events \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "split", "occurred_on": "2026-06-10", "split_numerator": 2, "split_denominator": 1}'
```

In the admin interface, use **Record split** on the position's row in the
Positions tab. An agent calls
`record_split(account, symbol, ratio="2:1", effective_date="2026-06-10")`.

- **Shares.** Every share held the day before becomes that many shares, at
  the start of the effective date: 40 shares in a 2-for-1 split become 80.
  The result is rounded to six decimal places. Other events dated that day
  are in post-split shares.
- **Basis.** The total cost basis stays the same, so the basis per share is
  divided by the ratio. If you track tax lots, every lot keeps its own total
  basis, acquisition date and holding period. The lots of a position whose
  shares are all in lots still hold exactly what the position holds. A lot's
  `quantity` and `basis_per_share` stay as acquired, and its remaining
  shares are shown in today's shares.
- **Value.** Closes from the provider are stored as published, not
  adjusted for later splits. A close dated before the split, such as the
  previous trading day's close before the effective date's close arrives,
  is divided by the ratio. So the position's value does not jump or dip on
  the effective date.

### Cash in lieu of a fractional share

A reverse split often leaves a fraction of a share, and many brokers pay
cash for it. Give that amount as `cash_in_lieu`, in cents. Purser then
records the fraction as a sale whose proceeds are exactly that cash, together
with the split: 25 shares in a 1-for-10 split become 2.5 shares, and the
0.5 share is sold for the cash.

```json
{"kind": "split", "occurred_on": "2026-06-10", "split_numerator": 1, "split_denominator": 10,
 "cash_in_lieu": "18.40", "lot_method": "fifo"}
```

The cash works like any sale's ([below](#sales-and-purchases-move-cash)). It
lands on the account's cash against `equity.securities`. If the account
already shows that cash, the call is refused, and you can link the broker's
row with `transaction_id` or override with `force_cash`. `settle_on` posts
the cash later. A position that tracks lots names the lots the fraction
comes from (`lot_method` or `lots`), and the realized gain is the cash less
that fraction's share of the basis. A split that leaves a whole number of
shares has no fraction, so `cash_in_lieu` is refused.

### Undo a wrong split

Reverse the split (`POST /position-events/<split id>/reverse`, or
`reverse_position_event(event_id)` from an agent). The shares, the lots'
shares and basis, and any cash in lieu come back exactly as they were before
the split. The cash-in-lieu sale is reversed with its split and never on its
own. Then record the right split. A split is refused on a position that held
nothing the day before, and only one split per position can take effect on
one day.

## Sales and purchases move cash

A sale puts its net proceeds (quantity x price - fees, rounded to the cent) on
the account on the trade date, the day its shares leave the holdings, so net
worth does not dip in between. To post the cash on a later settlement date
instead, give `settle_on` (`settle_date` from an agent). From an agent,
`record_sale` records it; check first with `dry_run=true`, which answers with
the shares before and after, the cash and its date, and writes nothing:

```text
record_sale(account="assets.plan", symbol="ACME", quantity="20",
            price="101.25", trade_date="2026-10-01", fees="4.95",
            dry_run=true)
```

Then the same call without `dry_run`. Over REST, record a `sell` event on the
position:

```bash
curl -X POST https://purser.example.com/positions/<position id>/events \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "sell", "occurred_on": "2026-10-01", "quantity": "20",
       "price": "101.25", "fees": "4.95"}'
```

- The cash is one ordinary transaction, posted, booked against the system
  account `equity.securities`. That account is not a category: the proceeds
  never count as income or spending, and net worth does not move: by default
  the cash posts on the trade date, the day the shares leave the holdings. (A
  later `settle_on`, or a linked bank row dated after the trade, leaves net
  worth short by the sale's value until that date; see below.) The register
  shows it as **Securities trade**.
- `proceeds_to: "none"` records a sale without cash, such as shares withheld
  for tax.
- Trade cash is in US dollars only (`equity.securities` is a USD account, and
  Purser keeps one currency per transaction). On an account in another
  currency, a trade that would move cash is refused: record it with
  `proceeds_to: "none"` or `funded_from: "none"`.
- A buy takes its cost (quantity x price + fees, rounded to the cent) off the
  account only with `funded_from: "cash"`. By default a buy moves no cash:
  plan purchases are usually funded by payroll, from outside the account.

### When the account already shows the cash

Many brokers send the sale's cash through the feed ("SOLD ..."). Purser looks
for a row of exactly that amount on the account, pending or posted, with one
other leg, a category (so not a transfer or a split), dated from the trade to
five days after settlement (`settle_on`, or the next business day when you give
none). When it finds one, the sale is refused with
`cash_already_recorded` (a 409 over REST, a tool error led by that code from
`record_sale`), naming the row, so the cash is never counted twice; a
`record_sale` dry run reports the row as `already_recorded`. If the row is still
pending, record the sale after the bank posts it and link the posted row. If
that row is the sale's cash, link it (`transaction_id="<the row>"` on `record_sale`, or over
REST):

```bash
curl -X POST https://purser.example.com/positions/<position id>/events \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind": "sell", "occurred_on": "2026-10-01", "quantity": "20",
       "price": "101.25", "transaction_id": "<the row the 409 named>"}'
```

which re-books that row to `equity.securities` as the sale's cash (the row is
corrected through a reversal pair; its amount and date stay the bank's, and
the sale settles on that date). The shares still leave on the trade date; until
the bank's date the sale's cash counts as in transit (see below), so net worth
does not dip. A link takes only the row that holds exactly
this sale's cash: its net proceeds to the cent (so give the price, and the
fees that produced the bank's figure), dated from the trade to five days after
the next business day (`settle_on` does not widen a link's window), and matched
to no bill. Any other row is refused (422). When the row's bank line settles a
bill, the refusal names the match: reject it if it is a proposal, unmatch it if
it is confirmed, then link.

If the row is a different payment that merely has the same amount, record the
sale again with `force_cash: true` (`force_cash=true` on `record_sale`):
Purser posts the sale's cash as well, and the trade's audit record notes the
override and the row it went past. `record_sale` names that row too, as
`cash_probe_overridden`, in its answer and in a dry run with `force_cash`.

A trade's cash moves only with its trade. Its row cannot be edited or
recategorized, merged into a transfer or matched to a bill, and neither can
the bank row a trade linked. Reversing the sale undoes the cash too, the way
every posted row is corrected (a reversal and its replacement): Purser's own
row is corrected to zero, so the register keeps it at 0.00 as the record of
the undo, and a linked row gets its old category back.

### When the broker's row arrives after the sale

If you record the sale before the broker's "SOLD" row reaches Purser, Purser
posts the cash itself. When the feed then delivers a row of exactly that
amount on the account, dated from the trade to five days after settlement
(the later of `settle_on` and the next business day), the import links it as
the sale's cash in place of Purser's own row, the same way `transaction_id`
links one: the cash is counted once, and the row keeps the bank's date and
description. Purser's row stays in the history as void, pointing at the
bank's row. The sync result counts these rows as `linked_trade_cash`. While
the broker's row is still pending it is not imported at all
(`skipped_trade_cash_pending`); the posted row takes over when the bank books
it. This works on every import, including an outage repair and a manual
refetch. Reversing the sale later gives the bank's row back the category it
was imported with, so the bank's money stays on the account as an ordinary
deposit.

A row outside that window, or of another amount, imports as an ordinary row.
If that leaves the cash counted twice, reverse the sale's event and record it
again with `transaction_id` naming the feed's row. On an account that has
"Feed balance is cash" on, the drift check shows such a gap.

### Cash in transit

The shares leave the holdings on the trade date, but the cash can land later:
on `settle_on` when you give a later date, or on the bank's date when the
cash is the broker's row. Until the cash lands, Purser counts it as in
transit: the position's holdings row carries it in its `market_value`, and
`list_holdings` and `GET /holdings` show it as `in_transit` (a sale's
proceeds positive, a cash-funded buy's cost negative). Net worth therefore
stays the same across a sale at the market price, on every day, including
weekends between a Friday trade and a Monday bank row. After a sale of every
share, the position still shows a zero-share line worth the cash until the
cash date. `unrealized_gain` covers the shares only.

## Check a cash sweep's balance

Some plan accounts reach SimpleFIN as a cash sweep: the feed's balance is the
sweep's cash, not the account's value. For such an account, an admin can turn
on **Feed balance is cash** (admin only; `PATCH /accounts/<id>` with
`{"feed_balance_is_cash": true}`). Each sync then compares the feed's balance
with the account's transactions, exactly as it does for a checking account,
and reports drift in **Settings -> Connections**, as `balance.drift` events and in
`whats_new`.

That is how a missing sale shows up: while the proceeds that funded the
disbursements are unrecorded, the account's transactions fall short of the
sweep's balance by exactly that much. Recording the sales closes the gap.

The setting needs manual holdings and turns off when the account goes back to
the feed. It is off by default because many brokers report the account's total
value as its balance.

## Prices

Each position is valued at the newest close for its symbol, from three
sources:

1. a close you record: `record_price(symbol, date, close)` or
   `POST /prices`;
2. the daily provider fetch, when a provider is configured;
3. a price implied by a feed snapshot of the same symbol in another account
   (its market value over its quantity).

The newest date wins. The order above only breaks a tie between closes of the
**same** date, so a close you recorded months ago never outlives newer ones.

A feed-implied price is dated by the day the bank was fetched, not the trading
session it reflects, so it ranks lower: it wins over a close you recorded or
the provider's only when that close is more than seven days older. And while
the bank keeps reporting the same value, the price keeps the date it first
appeared, so a bank whose value is frozen turns stale like any other price.

A position with no price at all adds **nothing** to the balance, and the
surfaces say so (`no_price` in `list_holdings`, "no price" in the UI). It is
never counted as zero. Neither does a position whose value at its close is too
large to store, which almost always means a mistyped share count: the
surfaces flag it as `value_out_of_range` ("check the share count" in the UI),
and recording a close does not change it; correct the count instead.

### Configure a provider

**Settings -> Market data** sets the provider, its API key and the staleness
threshold. Market data prices the holdings of every account that isn't
connected through SimpleFIN; SimpleFIN accounts are valued by the bank on
each sync. The page shows when the daily job last ran and runs next, and
names the symbols whose price is stale or missing. Every holding of every
brokerage account, with its price, is on the **Holdings** page
(`GET /holdings/portfolio`); a price entered by hand is marked
**manual price** (`manual_price`). The settings:

- **Provider**: `none` (the default: no provider; enter closes by hand), `tiingo` or
  `eodhd`, both end-of-day closes with a free key from the provider's site.
  Tiingo covers US stocks, ETFs and mutual funds. EODHD covers exchanges
  worldwide (`VOD.LSE`, `SHOP.TO`) and US mutual funds; its free key allows
  20 requests a day and one year of history, and a paid plan lifts both.
  London (`.LSE`) closes are in pence. The provider sees ticker symbols
  only, never share counts, accounts or values.
- **Fallback provider**: optional, the other provider, with its own key. A
  date range the provider does not know a ticker for, or fails to answer
  (a refused key, its daily limit), is asked of the fallback, and counts as
  answered once either provider answered it. A provider's not-found is no
  failure when a fallback is set; any other provider failure is still
  reported.
- **API key**: write-only and encrypted in Purser's database. There is no
  environment variable for it, and it never appears in a response or a log.
  A key with a space, a line break or a non-ASCII character is refused when
  you save it. A new key is tested before it is saved: Purser asks the
  provider for a few recent closes of one well-known symbol, and a key the
  provider refuses is not saved. **Test connection** tests the saved key the
  same way (`POST /prices/test`, admin; nothing is stored). A key goes only to
  the provider you entered it for: after you choose another provider, enter
  that provider's key. Swapping the provider and the fallback keeps each key
  with its own provider.
- **Stale after**: a position whose newest close is older than this many days
  (default 7) is flagged `stale`.

The fetch runs daily at 06:40 UTC. It fetches every date your positions hold
shares (from a week before each position's first day, so a weekend start
gets the Friday close) that the provider has not answered yet, including
positions you have closed, and later only what is new. When the history
grows (you backdate an event, or another account held the symbol at another
time), the next run fetches the missing dates, wherever they fall. Dates the
provider has nothing for (before a symbol listed, a weekend) are asked for
once. **Fetch now** runs the same job on demand. One failing ticker, or one
failing date range, never blocks the others: it records a
`price.fetch_failed` event and the rest carry on.

Tiingo spells share classes with a dash, so a position recorded as `BRK.B` is
fetched as `BRK-B`. EODHD adds the exchange: a bare ticker is a US listing
(`AAPL` is fetched as `AAPL.US`, `BRK.B` as `BRK-B.US`), and a symbol with
a longer suffix is taken as `TICKER.EXCHANGE` already (`VOD.LSE`).

### Symbol settings

**Symbol settings** on the same page (`PUT /prices/symbols/{symbol}`, write
scope; `GET /prices/symbols`) covers what a provider's own spelling cannot:

- **A ticker per provider**: the symbol as that provider spells it, such as
  `VOD.LSE` at EODHD for a position recorded as `VOD`. Changing a ticker
  makes that provider ask for the symbol's dates again.
- **Don't fetch prices for this symbol** (`skip_providers`): the symbol is
  never sent to a provider. Use it for a plan-only fund or a ticker no
  provider carries, so the daily job does not ask for it; enter its closes
  by hand as below.

## Price a plan-only fund

Some plans' feeds send a fund they hold but cannot value: an employer
plan's collective trust, say, which no market-data provider prices. The
feed reports the shares with a market value of `0`, or with none at all, so
the position would add nothing to the balance. You do not need to enter that account's
holdings in Purser for it: record the fund's unit price, and Purser values the feed's row from
it on read.

```text
record_price(symbol="ACMEX", date="2026-10-16", close="23.4")
```

or over REST:

```bash
curl -X POST https://purser.example.com/prices \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"symbol": "ACMEX", "date": "2026-10-16", "close": "23.4"}'
```

- Record the price under the fund's ticker. The feed's symbol is matched
  the way Purser stores a ticker (spaces trimmed, upper case), so `acmex`
  in the feed takes the price recorded for `ACMEX`.
- A feed that sends a description instead of a ticker (`Stable Value
  Trust`, say) cannot be priced: no recorded price matches it. Such a
  position is flagged `unpriceable` (in `list_holdings`, `GET /holdings` and
  `whats_new`) and adds nothing; there is nothing to record for it.
- Any feed row of that symbol that holds shares with no value (`0` or none) is worth
  shares × the newest price dated on or before the row's date. A price you
  record wins over a provider's of the same date. The feed's stored row
  never changes, and a value the feed does send is never overridden.
- Recording a price for an earlier date re-values the account's history
  from that date; net worth heals by itself.
- Weekly is enough: the newest price carries forward, and once it is older
  than the staleness threshold (Settings, Market data) the fund is reported stale
  like a position entered in Purser: `stale` in `list_holdings`, a `price.stale`
  event, Settings -> Market data and `whats_new`'s `stale_prices`.
- Until a price exists, the position shows **no price** (`no_price` in
  `list_holdings`, `market_value` null), never a silent 0, and `whats_new`
  reports it once (`unpriced_holdings`).
- With a provider configured, the daily job asks it for the fund's ticker
  too: the close in effect on each such row's date. An ordinary fund or ETF
  the feed reports at 0 is then priced without typing anything. A ticker the
  provider does not know (a plan-only collective trust, say) is not an
  error: the run lists it under `not_covered`, and its price comes from the
  plan's site, recorded as above.

## Tax lots and equity awards

A position entered in Purser can also track its tax lots (acquisition date,
remaining shares, basis), and the account its equity awards and vest
schedule. A position's basis then comes from its lots, never an average, and
a sale names the lots it draws on. Lot lists and award schedules load in bulk
with a previewed, idempotent capture. See
[Track tax lots and equity awards](../../how-to/lots-and-awards/).

## Where the value shows up

Positions entered in Purser are written into the same `holdings_snapshots` table
the feed fills, so everything that reads holdings picks them up unchanged:

- the account's balance is its cash plus its holdings' market value
  (`balance_method: "postings+holdings"`), on every account surface;
- `net_worth`, `balance_history` and the Grafana dashboards include it;
- `list_holdings` lists each brokerage account with its `source`, its value,
  and how many positions are on a stale close (`stale_price`) or have no price
  (`no_price`); named one account, it pages through its positions with their
  cost basis, in symbol order (`cursor`, `next_cursor`). A page never repeats
  a position, but each page reads the account's newest set when it is
  called: a position added meanwhile with a symbol before the cursor is not
  listed, and the daily price job can move later pages to a newer set;
- `whats_new` reports symbols whose price turned stale (`stale_prices`) and
  feed positions no price values yet (`unpriced_holdings`);
- the account page in the admin interface shows a **Holdings** table for
  every brokerage account, entered in Purser or from SimpleFIN: each holding's
  value, unrealized gain and share of the account, a cash row and the total
  (the account's balance), with stale, unpriced and in-transit lines marked.
  It reads `GET /accounts/{id}/holdings/summary`, which computes every figure
  on the server. Its cost basis and gain total only the holdings whose gain
  is known, so a holding with no basis never counts as all gain.

See [Balances](../../explanation/balances/) for how the postings and holdings
figures combine, and [Events and webhooks](../../reference/events-webhooks/) for
`position.updated`, `price.stale`, `price.fetch_failed` and
`holding.unpriced`. Agents record share counts with `record_position`, sales
with `record_sale` and splits with `record_split`, and undo a wrong one with
`reverse_position_event`
(reopening a closed position first with `reopen_position`); buys and closing a
position are REST (or the admin UI's Positions tab, for share counts and
closing).
