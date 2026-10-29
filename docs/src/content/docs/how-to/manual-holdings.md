---
title: Track holdings by hand
description: Record what a brokerage account holds when its feed sends cash but no holdings, keep those positions priced, and see how they reach balances and net worth.
---

Some brokerages send an account's cash through SimpleFIN but no holdings. An
employee stock plan is the usual case: the shares sit at the broker, the feed
shows only the cash that leaves. Without holdings, the account reports its cash
alone and net worth leaves the shares out.

Purser can track such an account's holdings by hand. You (or your agent)
record what the account holds, Purser prices it every day, and the value
reaches the account's balance and net worth exactly like a feed snapshot would.

## Switch the account to manual holdings

An account's holdings come from one source at a time: the feed (the default) or
positions recorded by hand. An admin switches a brokerage account, once:

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
  hand-tracked snapshot is dated the day after the last feed one, so no date
  mixes the two sources.
- Switching back to `feed` needs the account to be mapped to a feed account
  (a 422 otherwise) and changes no snapshot: the hand-tracked holdings stay
  in effect, so the account keeps its value, until the first sync after the
  switch ends the hand-tracked period. From that sync's date the account
  holds what the feed reports, and a position the feed does not report ends
  there (all of them, for a feed that sends only cash). The sync result counts
  what it ended (`manual_period_tombstones`), and the audit log records it as
  `account.manual_period_end`. The hand-tracked history before stays, and so
  do your positions and their events, read-only, until the account is
  switched to manual again. Unmapping or closing the account before that sync
  ends the period the same day; remapping it onto another feed account does
  not (that mapping's first sync ends it). If no sync of the account arrives
  for the price staleness threshold (7 days by default), the daily price job
  reports it once (`holding.period_open`, and a `whats_new` line): the
  hand-tracked holdings are still counted, unpriced by anything new, so check
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
| `buy` | It gained this many shares; with a `price`, the cost basis grows by quantity × price + fees. |
| `sell` | It lost this many shares; the basis drops at average cost. |

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

**Config -> Prices** sets the provider, its API key and the staleness
threshold:

- **Provider**: `none` (the default: record closes by hand) or `tiingo`
  (end-of-day closes; a free key is enough). The provider sees ticker symbols
  only, never quantities, accounts or values.
- **API key**: write-only and encrypted in Purser's database. There is no
  environment variable for it, and it never appears in a response or a log.
  A key with a space, a line break or a non-ASCII character is refused when
  you save it.
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
fetched as `BRK-B`.

## Price a plan-only fund

Some plans' feeds send a fund they hold but cannot value: an employer
plan's collective trust, say, which no market-data provider prices. The
feed reports the shares with a market value of `0`, or with none at all, so
the position would add nothing to the balance. You do not need to track that account by hand
for it: record the fund's unit price, and Purser values the feed's row from
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
  than the staleness threshold (Config, Prices) the fund is reported stale
  like a hand-tracked one: `stale` in `list_holdings`, a `price.stale`
  event, Config -> Prices and `whats_new`'s `stale_prices`.
- Until a price exists, the position shows **no price** (`no_price` in
  `list_holdings`, `market_value` null), never a silent 0, and `whats_new`
  reports it once (`unpriced_holdings`).
- No provider is asked for such a symbol: the price comes from the plan's
  site.

## Where the value shows up

Hand-tracked positions are written into the same `holdings_snapshots` table
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
  feed positions no price values yet (`unpriced_holdings`).

See [Balances](../../explanation/balances/) for how the postings and holdings
figures combine, and [Events and webhooks](../../reference/events-webhooks/) for
`position.updated`, `price.stale`, `price.fetch_failed` and
`holding.unpriced`.
